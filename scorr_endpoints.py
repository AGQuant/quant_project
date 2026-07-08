"""
Scorr Query Endpoints — FastAPI
Smart routing: Cache (0 tokens) → Anthropic API (only for explanations)
Monthly cost: $2-3 (vs $100 Max plan)
"""

from fastapi import APIRouter, Body
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta, time as dt_time
import os
import time
import psycopg
import httpx

from nse_holidays import is_trading_day   # cc#193: market-hours gate for live-LTP


def _market_open_ist() -> bool:
    """cc#193: True only during a real NSE session — trading day + 09:15-15:30 IST.
    Off-hours the M2M card must serve the last futures session bar, never treat a
    stale/phantom tick as live."""
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return is_trading_day(now.date()) and dt_time(9, 15) <= now.time() <= dt_time(15, 30)

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

def get_conn():
    return psycopg.connect(DATABASE_URL)


# ── Request Models ───────────────────────────────────────────────────────────────────────────────

class ScorrQueryRequest(BaseModel):
    type: str
    segment: Optional[str] = None
    threshold: Optional[float] = None
    limit: Optional[int] = 20
    stocks: Optional[List[str]] = None
    include_explanation: Optional[bool] = False


# ── Cache Helpers ───────────────────────────────────────────────────────────────────────────────

def is_cache_fresh(max_age_minutes: int = 15) -> bool:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT last_sync FROM cache_metadata WHERE key = 'gvm_cache'")
            row = cur.fetchone()
            if not row or not row[0]: return False
            return (time.time() - row[0].timestamp()) / 60 < max_age_minutes
    except Exception:
        return False


def get_top_stocks_native(limit: int = 20, segment: str = None) -> list:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if segment:
                cur.execute("SELECT symbol, gvm_score, growth, value, momentum, segment FROM gvm_cache WHERE segment = %s AND gvm_score > 0 ORDER BY gvm_score DESC LIMIT %s", (segment, limit))
            else:
                cur.execute("SELECT symbol, gvm_score, growth, value, momentum, segment FROM gvm_cache WHERE gvm_score > 0 ORDER BY gvm_score DESC LIMIT %s", (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


def filter_by_threshold_native(segment: str, threshold: float) -> list:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT symbol, gvm_score, growth, value, momentum, segment FROM gvm_cache WHERE segment = %s AND gvm_score >= %s ORDER BY gvm_score DESC", (segment, threshold))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


def get_peer_comparison_native(symbol: str, segment: str) -> dict:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT gvm_score FROM gvm_cache WHERE symbol = %s", (symbol,))
            stock = cur.fetchone()
            cur.execute("SELECT avg_gvm FROM peer_averages WHERE segment = %s", (segment,))
            peer = cur.fetchone()
            if not stock or not peer: return {"symbol": symbol, "error": "not found in cache"}
            score = float(stock[0]); peer_avg = float(peer[0])
            percentile = round((score / peer_avg) * 100, 1)
            return {"symbol": symbol, "gvm_score": score, "peer_avg": peer_avg, "percentile": percentile,
                    "vs_peers": "outperforming" if percentile > 110 else "underperforming" if percentile < 90 else "in_line"}
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


# ── Anthropic API Call ───────────────────────────────────────────────────────────────────────────

async def call_anthropic_api(prompt: str) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "tokens_used": 0}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
            )
            data = response.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            usage = data.get("usage", {})
            it, ot = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            return {"text": text, "tokens_used": it + ot, "input_tokens": it, "output_tokens": ot, "cost_usd": round((it * 1 + ot * 5) / 1_000_000, 6)}
    except Exception as e:
        return {"error": str(e), "tokens_used": 0}


# ── Main Endpoint ───────────────────────────────────────────────────────────────────────────

@router.post("/api/scorr/query")
async def scorr_query(req: ScorrQueryRequest):
    start_time = time.time(); tokens_used = 0; api_calls = 0; result = None

    if req.type == "top_stocks":
        result = get_top_stocks_native(req.limit or 20, req.segment)
    elif req.type == "filter":
        if not req.segment or req.threshold is None: return {"error": "segment and threshold required"}
        result = filter_by_threshold_native(req.segment, req.threshold)
    elif req.type == "peer_compare":
        if not req.stocks or not req.segment: return {"error": "stocks and segment required"}
        result = [get_peer_comparison_native(s.upper(), req.segment) for s in req.stocks]
    elif req.type == "recommendation":
        if not req.stocks: return {"error": "stocks required"}
        scores = []
        with get_conn() as conn, conn.cursor() as cur:
            for symbol in req.stocks:
                cur.execute("SELECT symbol, gvm_score, growth, value, momentum, segment FROM gvm_cache WHERE symbol = %s", (symbol.upper(),))
                row = cur.fetchone()
                if row: scores.append({"symbol": row[0], "gvm_score": float(row[1]), "growth": float(row[2]), "value": float(row[3]), "momentum": float(row[4]), "segment": row[5]})
                else: scores.append({"symbol": symbol, "error": "not in cache"})
        result = {"scores": scores}
        if req.include_explanation:
            api_response = await call_anthropic_api(f"Analyze these Indian stocks:\n{scores}\n\nGive a brief recommendation (2-3 lines per stock). Focus on GVM score interpretation and key risks.")
            result["explanation"] = api_response.get("text", "")
            tokens_used = api_response.get("tokens_used", 0); api_calls = 1
    else:
        return {"error": f"Unknown query type: {req.type}"}

    return {"type": req.type, "result": result, "meta": {"api_calls": api_calls, "tokens_used": tokens_used, "cache_used": api_calls == 0, "duration_ms": round((time.time() - start_time) * 1000, 1), "cache_fresh": is_cache_fresh()}}


# ── SmartGain M2M (cc_task #93) — Live LTP from intraday_prices ─────────────

@router.get("/api/smartgain/m2m")
def smartgain_m2m():
    """SmartGain MHK40 holdings — M2M with live LTP from the live futures feed.

    cc#132: open book sourced from smartgain_holdings (broker-reconciled entry_price).
    personal_journal entry_price can drift from the actual filled price; holdings
    carries the correct cost basis.

    cc#147 (BUG-1): MHK40 trades FUTURES but LTP was priced from cmp_prices SPOT,
    causing a constant drift of basis*qty vs the broker MTM. Pricing is now
    FUT-LTP-FIRST: latest fyers_fut 5m bar close when fresh (<=10 min), else a
    synthetic fut price (spot + latest futures_basis.basis), else spot alone,
    else the last intraday tick of any source. pricing_method on each row shows
    which path was used.

    cc#161 (safety fix): the spot_only/eod fallback paths had NO staleness or
    existence check -- confirmed live 03-Jul a NIFTY position showed CMP from a
    cmp_prices row 35+ days old (root cause: NIFTY futures are never actually
    subscribed on the live feed -- futures_basis has zero rows ever for
    NIFTY/BANKNIFTY -- see cc#162) as if it were a real live price, producing a
    wildly wrong MTM. Now: spot_only/eod are downgraded to pricing_method=
    "unavailable" (ltp/mtm=null, reason set) when either (a) the candidate tick
    is >24h old, or (b) this symbol has NEVER had a single fyers_fut row (a
    structurally fut-less instrument, where spot is not a valid stand-in for
    futures price regardless of freshness -- e.g. index futures pre-cc#162).
    """
    try:
        mkt_open = _market_open_ist()   # cc#193: off-hours -> fut_eod (last session close)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH open_book AS (
                    -- cc#132: use smartgain_holdings for broker-reconciled entry_price.
                    -- personal_journal.entry_price can differ from the actual fill
                    -- (e.g. SONACOMS 614.40 journal vs 614.63 broker) causing MTM drift.
                    -- cc#312: source_tag = Arpit's editable source override.
                    SELECT id, symbol, direction, qty, entry_price, source_tag
                    FROM smartgain_holdings
                    WHERE account = 'MHK40'
                )
                SELECT
                    h.symbol, h.direction, h.qty,
                    ROUND(h.entry_price::numeric, 2)                         AS entry_price,
                    ROUND(lp.live_ltp::numeric, 2)                          AS ltp,
                    ROUND(
                        CASE h.direction
                            WHEN 'LONG'  THEN (lp.live_ltp - h.entry_price) * h.qty
                            WHEN 'SHORT' THEN (h.entry_price - lp.live_ltp) * h.qty
                            ELSE 0
                        END::numeric, 2
                    )                                                         AS mtm,
                    lp.pricing_method                                        AS pricing_method,
                    lp.is_live                                              AS is_live,
                    ROUND(lp.ltp_age_min::numeric, 1)                        AS ltp_age_min,
                    ROUND(lp.spot_ltp::numeric, 2)                          AS spot_ltp,
                    ROUND(lp.fut_ltp_synthetic::numeric, 2)                  AS fut_ltp_synthetic,
                    ROUND(lp.basis_age_min::numeric, 1)                      AS basis_age_min,
                    lp.last_tick                                            AS last_tick,
                    lp.fut_ever_existed                                     AS fut_ever_existed,
                    -- cc#304: auto-derived V8 basket tag. LIVE VLOOKUP to V8 open paper
                    -- positions (symbol + side match, status=OPEN). NULL when V8 is not
                    -- currently in this symbol+side -> card shows no tag (blank on no-match,
                    -- Arpit's choice). Dynamic: disappears if V8 later closes its paper leg.
                    (SELECT vp.basket FROM v8_paper_positions vp
                      WHERE vp.symbol = h.symbol AND vp.side = h.direction
                        AND vp.status = 'OPEN' LIMIT 1)                      AS v8_basket,
                    h.source_tag                                            AS source_tag   -- cc#312
                FROM open_book h
                LEFT JOIN LATERAL (
                    SELECT
                        c.spot_ltp, c.basis, c.fut_close,
                        (c.spot_ltp + c.basis)                                          AS fut_ltp_synthetic,
                        EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.basis_ts))/60.0   AS basis_age_min,
                        CASE
                            -- cc#123/cc#147: "live" fut bar within the last 10 min wins.
                            WHEN c.fut_close IS NOT NULL
                             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10
                                THEN 'fut_live'
                            -- cc#193: OFF-HOURS -> last futures session close (not spot).
                            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN 'fut_eod'
                            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL THEN 'synthetic'
                            WHEN c.spot_ltp IS NOT NULL THEN 'spot_only'
                            WHEN c.eod_close IS NOT NULL THEN 'eod'
                            ELSE NULL
                        END AS pricing_method,
                        CASE
                            WHEN c.fut_close IS NOT NULL
                             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10
                                THEN c.fut_close
                            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN c.fut_close   -- cc#193 off-hours
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
                            -- cc#147: fut_live is always live; synthetic requires the
                            -- basis itself to be fresh (<=30 min) or it's flagged stale.
                            WHEN c.fut_close IS NOT NULL
                             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10
                                THEN true
                            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN false   -- cc#193 fut_eod is real but not "live"
                            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL
                                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.basis_ts))/60.0 <= 30
                            ELSE false
                        END AS is_live,
                        COALESCE(
                            CASE WHEN c.fut_close IS NOT NULL
                             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10
                                THEN c.fut_ts END,
                            CASE WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN c.fut_ts END,   -- cc#193 off-hours as-of = bar time
                            CASE WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL THEN c.basis_ts END,
                            CASE WHEN c.spot_ltp IS NOT NULL THEN c.spot_ts END,
                            c.eod_ts
                        ) AS last_tick,
                        c.fut_ever_existed
                    FROM (
                        SELECT
                            -- cc#193: only TRADING-SESSION fut bars (weekday +
                            -- 09:15-15:30 IST) — never a phantom off-hours tick.
                            (SELECT ip.close FROM intraday_prices ip
                              WHERE ip.symbol = h.symbol AND ip.source = 'fyers_fut'
                                AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                                AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
                              ORDER BY ip.ts DESC LIMIT 1)                    AS fut_close,
                            (SELECT ip.ts FROM intraday_prices ip
                              WHERE ip.symbol = h.symbol AND ip.source = 'fyers_fut'
                                AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                                AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
                              ORDER BY ip.ts DESC LIMIT 1)                    AS fut_ts,
                            (SELECT cp.cmp::numeric FROM cmp_prices cp
                              WHERE cp.symbol = h.symbol)                    AS spot_ltp,
                            (SELECT cp.updated_at FROM cmp_prices cp
                              WHERE cp.symbol = h.symbol)                    AS spot_ts,
                            (SELECT fb.basis FROM futures_basis fb
                              WHERE fb.symbol = h.symbol
                              ORDER BY fb.ts DESC LIMIT 1)                    AS basis,
                            (SELECT fb.ts FROM futures_basis fb
                              WHERE fb.symbol = h.symbol
                              ORDER BY fb.ts DESC LIMIT 1)                    AS basis_ts,
                            (SELECT ip.close FROM intraday_prices ip
                              WHERE ip.symbol = h.symbol
                              ORDER BY ip.ts DESC LIMIT 1)                    AS eod_close,
                            (SELECT ip.ts FROM intraday_prices ip
                              WHERE ip.symbol = h.symbol
                              ORDER BY ip.ts DESC LIMIT 1)                    AS eod_ts,
                            -- cc#161: existence check, NOT scoped to today/recency --
                            -- distinguishes a structurally fut-less instrument (index
                            -- futures never subscribed, e.g. NIFTY -- see cc#162) from
                            -- a normal stock future momentarily missing a fresh tick.
                            EXISTS(
                                SELECT 1 FROM intraday_prices ip4
                                WHERE ip4.symbol = h.symbol AND ip4.source = 'fyers_fut'
                                LIMIT 1
                            )                                                 AS fut_ever_existed
                    ) c
                ) lp ON true
                ORDER BY h.id
            """, {"mkt_open": mkt_open})
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                row["entry_price"]        = float(row["entry_price"]) if row["entry_price"] is not None else None
                row["ltp"]                = float(row["ltp"])         if row["ltp"]         is not None else None
                row["mtm"]                = float(row["mtm"])         if row["mtm"]         is not None else None
                row["is_live"]            = bool(row["is_live"])
                row["ltp_age_min"]        = float(row["ltp_age_min"]) if row["ltp_age_min"] is not None else None
                row["spot_ltp"]           = float(row["spot_ltp"]) if row["spot_ltp"] is not None else None
                row["fut_ltp_synthetic"]  = float(row["fut_ltp_synthetic"]) if row["fut_ltp_synthetic"] is not None else None
                row["basis_age_min"]      = float(row["basis_age_min"]) if row["basis_age_min"] is not None else None
                # cc#123: last_tick = the actual live-feed tick time (IST), so the UI can
                # show real data age instead of a wall-clock that always reads "now".
                row["last_tick"]   = row["last_tick"].isoformat() if row["last_tick"] else None
                row["updated_at"]  = row["last_tick"]   # back-compat: old key now = live tick

                # cc#161: safety downgrade -- spot_only/eod are the two paths with no
                # built-in freshness/relevance guarantee. Never present a number derived
                # from them as real MTM when (a) the tick is stale (>24h), or (b) this
                # symbol has never had a single real futures tick (spot is not a valid
                # stand-in for futures price for such an instrument, regardless of how
                # fresh the spot number itself is -- e.g. NIFTY/BANKNIFTY pre-cc#162).
                fut_ever = bool(row.pop("fut_ever_existed", False))
                reason = None
                if row["pricing_method"] in ("spot_only", "eod"):
                    if not fut_ever:
                        reason = "no_live_futures_feed"
                    elif row["ltp_age_min"] is not None and row["ltp_age_min"] > 24 * 60:
                        reason = f"stale_data_{round(row['ltp_age_min'] / 1440)}d"
                    if reason:
                        row["pricing_method"] = "unavailable"
                        row["ltp"] = None
                        row["mtm"] = None
                        row["is_live"] = False
                elif row["pricing_method"] is None:
                    reason = "no_data"
                    row["pricing_method"] = "unavailable"
                    row["is_live"] = False
                row["reason"] = reason
                rows.append(row)
            # ── UNREALISED: live MTM on open positions (existing computation) ──
            unrealised   = round(sum(r["mtm"] or 0 for r in rows), 2)
            last_updated = max((r["last_tick"] for r in rows if r["last_tick"]), default=None)
            any_live     = any(r["is_live"] for r in rows)

            # ── REALISED: closed-trade P&L this week ──
            # cc#237: derive from the FIFO replay (smartgain_orders + opening), NOT a raw
            # personal_journal SUM. personal_journal has no account column and mixes SmartGain
            # closes with Arpit's other trades (cross-contamination), and BUG C left it empty
            # so this tile read +0.00 all session. All three realised endpoints (/m2m,
            # /daily_m2m week card, /daily_m2m?range=1w) now read this same replay -> identical.
            from smartgain_daily_m2m import current_week_realised, current_week_brokerage
            realised = current_week_realised("MHK40")

            # ── GROSS: realised + unrealised ──
            gross = round(realised + unrealised, 2)

            # ── cc#301: brokerage (this week, live estimate) + NET = gross - brokerage.
            # Standing rule: Gross / Brokerage / Net must always be shown as three separate
            # line items, never netted silently. `total` stays = gross for byte-compat with
            # older consumers; the web surfaces now headline `net`.
            brokerage = current_week_brokerage("MHK40")
            net = round(gross - brokerage, 2)

            return {
                "account": "MHK40", "positions": rows,
                "realised": realised, "unrealised": unrealised, "total": gross,
                "gross": gross, "brokerage": brokerage, "net": net,   # cc#301
                "total_mtm": unrealised,  # back-compat: old field == unrealised bucket
                "position_count": len(rows), "last_updated": last_updated,
                "data_source": "live_fyers" if any_live else "holdings",
            }
    except Exception as e:
        return {"error": str(e)}


# ── cc#305: Client tracking (read-only, live) ─────────────────────────────────
# Lightweight LIVE view of external clients' open F&O positions (separate book from
# SmartGain MHK40). Data entry is done by Claude web via SQL into client_positions;
# this endpoint only reads + values them. NO realised/closed/orderbook — open + live P&L.

CLIENT_SCOPE = ["Trade Karo", "Phani", "Kartik", "Ashish", "Akshay"]


@router.get("/api/clients/positions")
def clients_positions():
    """Open client positions valued on FUTURES LTP. quantity = lots * futures lot_size;
    mtm = (LONG: fut_ltp-entry ELSE entry-fut_ltp) * lots * lot_size. CMP = fut_ltp
    (latest fyers_fut 5m trading-session close, same fut-LTP-first basis as SmartGain).
    Every in-scope client appears even with zero open positions (shows flat)."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT cp.id, cp.client, cp.symbol, cp.direction, cp.lots, cp.qty, cp.is_dabba,
                       ROUND(cp.entry_price::numeric, 2)                     AS entry_price,
                       fu.lot_size,
                       (SELECT ip.close FROM intraday_prices ip
                         WHERE ip.symbol = cp.symbol AND ip.source = 'fyers_fut'
                           AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                           AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
                         ORDER BY ip.ts DESC LIMIT 1)                        AS fut_ltp,
                       (SELECT ip.ts FROM intraday_prices ip
                         WHERE ip.symbol = cp.symbol AND ip.source = 'fyers_fut'
                           AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                           AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
                         ORDER BY ip.ts DESC LIMIT 1)                        AS fut_ts,
                       -- cc#312: same live V8-basket VLOOKUP as SmartGain + editable source_tag
                       (SELECT vp.basket FROM v8_paper_positions vp
                         WHERE vp.symbol = cp.symbol AND vp.side = cp.direction
                           AND vp.status = 'OPEN' LIMIT 1)                    AS v8_basket,
                       cp.source_tag                                        AS source_tag
                FROM client_positions cp
                LEFT JOIN futures_universe fu ON fu.symbol = cp.symbol
                WHERE cp.status = 'OPEN'
                ORDER BY cp.client, cp.symbol
            """)
            rows = cur.fetchall()

        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        positions, last_ts = [], None
        for (pid, client, symbol, direction, lots, raw_qty, is_dabba, entry, lot_size,
             fut_ltp, fut_ts, v8_basket, source_tag) in rows:
            lots = int(lots) if lots is not None else None
            entry = float(entry) if entry is not None else None
            lot_size = int(lot_size) if lot_size is not None else None
            ltp = float(fut_ltp) if fut_ltp is not None else None
            # cc#307: cp.qty is authoritative (supports dabba accounts w/ raw qty + lots=NULL).
            # Only fall back to lots*lot_size when qty was never stored.
            qty = int(raw_qty) if raw_qty is not None else (
                lots * lot_size if (lots is not None and lot_size is not None) else None)
            mtm = None
            if ltp is not None and entry is not None and qty is not None:
                per = (ltp - entry) if direction == "LONG" else (entry - ltp)
                mtm = round(per * qty, 2)
            age_min = None
            if fut_ts is not None:
                age_min = round((now_ist - fut_ts).total_seconds() / 60.0, 1)
                if last_ts is None or fut_ts > last_ts:
                    last_ts = fut_ts
            positions.append({
                "id": pid, "client": client, "symbol": symbol, "direction": direction,
                "lots": lots, "lot_size": lot_size, "qty": qty,
                "is_dabba": bool(is_dabba),
                "entry_price": entry, "cmp": round(ltp, 2) if ltp is not None else None,
                "mtm": mtm,
                "v8_basket": v8_basket, "source_tag": source_tag,   # cc#312
                "is_live": (age_min is not None and age_min <= 10 and _market_open_ist()),
            })

        # per-client summary — every in-scope client present (+ any extra client in the data)
        order = list(CLIENT_SCOPE)
        for p in positions:
            if p["client"] not in order:
                order.append(p["client"])
        summary = []
        for c in order:
            cps = [p for p in positions if p["client"] == c]
            summary.append({
                "client": c,
                "mtm": round(sum(p["mtm"] or 0 for p in cps), 2),
                "position_count": len(cps),
            })

        grand_mtm = round(sum(p["mtm"] or 0 for p in positions), 2)
        return {
            "clients": order,
            "positions": positions,
            "summary": summary,
            "grand_total_mtm": grand_mtm,
            "total_positions": len(positions),
            "last_updated": last_ts.isoformat() if last_ts else None,
            "data_source": "fyers_fut",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/clients/realised")
def clients_realised():
    """cc#306: booked client P&L from client_closed. pnl is precomputed by Claude web —
    we only SUM/group. Returns per-client total + date-wise breakup (each close_date ->
    date_total + the individual closes under it) + grand total. Every in-scope client
    appears even with zero realised; an empty table yields all-0.00 + total_closed=0."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT client, close_date, symbol, direction, lots, qty,
                       ROUND(entry_price::numeric, 2), ROUND(exit_price::numeric, 2),
                       ROUND(pnl::numeric, 2)
                FROM client_closed
                ORDER BY client, close_date DESC, id
            """)
            rows = cur.fetchall()

        clients_map = {}
        for client, close_date, symbol, direction, lots, qty, entry, exit_, pnl in rows:
            pv = float(pnl) if pnl is not None else 0.0
            cm = clients_map.setdefault(client, {"total": 0.0, "count": 0, "dates": {}})
            cm["total"] += pv
            cm["count"] += 1
            dm = cm["dates"].setdefault(str(close_date), {"date_total": 0.0, "closes": []})
            dm["date_total"] += pv
            dm["closes"].append({
                "symbol": symbol, "direction": direction,
                "lots": int(lots) if lots is not None else None,
                "qty": int(qty) if qty is not None else None,
                "entry_price": float(entry) if entry is not None else None,
                "exit_price": float(exit_) if exit_ is not None else None,
                "pnl": round(pv, 2),
            })

        order = list(CLIENT_SCOPE)
        for c in clients_map:
            if c not in order:
                order.append(c)

        by_client = []
        for c in order:
            cm = clients_map.get(c)
            if not cm:
                by_client.append({"client": c, "total_realised": 0.0, "position_count": 0, "dates": []})
                continue
            dates = [{"close_date": dk, "date_total": round(cm["dates"][dk]["date_total"], 2),
                      "closes": cm["dates"][dk]["closes"]}
                     for dk in sorted(cm["dates"].keys(), reverse=True)]   # newest date first
            by_client.append({
                "client": c, "total_realised": round(cm["total"], 2),
                "position_count": cm["count"], "dates": dates,
            })

        grand = round(sum(cm["total"] for cm in clients_map.values()), 2)
        return {
            "clients": order,
            "by_client": by_client,
            "grand_total_realised": grand,
            "total_closed": len(rows),
        }
    except Exception as e:
        return {"error": str(e)}


# ── cc#312: editable Source tag (UI-only writes; position data itself is owned by Claude web) ──

@router.post("/api/smartgain/position/source_tag")
def smartgain_set_source_tag(payload: dict = Body(...)):
    """Set Arpit's editable Source tag on a SmartGain open position (smartgain_holdings), keyed
    by id or symbol+direction. A blank/empty value clears it (card falls back to auto V8 basket,
    else Undefined). This is the ONLY UI write to source_tag; qty/P&L/reconcile are untouched."""
    tag = (str(payload.get("source_tag") or "").strip()) or None
    account = payload.get("account", "MHK40")
    pid = payload.get("id")
    sym, direction = payload.get("symbol"), payload.get("direction")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if pid is not None:
                cur.execute("UPDATE smartgain_holdings SET source_tag=%s, updated_at=NOW() "
                            "WHERE account=%s AND id=%s", (tag, account, pid))
            elif sym and direction:
                cur.execute("UPDATE smartgain_holdings SET source_tag=%s, updated_at=NOW() "
                            "WHERE account=%s AND symbol=%s AND direction=%s",
                            (tag, account, str(sym).upper(), str(direction).upper()))
            else:
                return {"error": "id or symbol+direction required"}
            n = cur.rowcount
            conn.commit()
        return {"ok": True, "updated": n, "source_tag": tag}
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/clients/position/source_tag")
def clients_set_source_tag(payload: dict = Body(...)):
    """Set the editable Source tag on a client open position (client_positions), keyed by id.
    Blank/empty clears it (falls back to auto V8 basket, else Undefined)."""
    tag = (str(payload.get("source_tag") or "").strip()) or None
    pid = payload.get("id")
    if pid is None:
        return {"error": "id required"}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE client_positions SET source_tag=%s, updated_at=NOW() WHERE id=%s", (tag, pid))
            n = cur.rowcount
            conn.commit()
        return {"ok": True, "updated": n, "source_tag": tag}
    except Exception as e:
        return {"error": str(e)}


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/api/scorr/health")
def scorr_health():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gvm_cache")
            gvm_count = cur.fetchone()[0]
            cur.execute("SELECT last_sync, status FROM cache_metadata WHERE key = 'gvm_cache'")
            meta = cur.fetchone()
        return {"status": "ok", "gvm_cache_count": gvm_count, "cache_last_sync": str(meta[0]) if meta else None,
                "cache_status": meta[1] if meta else "unknown", "cache_fresh": is_cache_fresh(),
                "anthropic_api_key_set": bool(ANTHROPIC_API_KEY)}
    except Exception as e:
        return {"status": "error", "error": str(e)}
