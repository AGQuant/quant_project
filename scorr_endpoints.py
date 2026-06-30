"""
Scorr Query Endpoints — FastAPI
Smart routing: Cache (0 tokens) → Anthropic API (only for explanations)
Monthly cost: $2-3 (vs $100 Max plan)
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List
import os
import time
import psycopg
import httpx

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
    """SmartGain MHK40 holdings — M2M with live LTP from the live CMP feed.

    cc#132: open book sourced from smartgain_holdings (broker-reconciled entry_price).
    personal_journal entry_price can drift from the actual filled price; holdings
    carries the correct cost basis. LTP is fetched live from cmp_prices/intraday
    so MTM is always current, not a stored snapshot.
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH open_book AS (
                    -- cc#132: use smartgain_holdings for broker-reconciled entry_price.
                    -- personal_journal.entry_price can differ from the actual fill
                    -- (e.g. SONACOMS 614.40 journal vs 614.63 broker) causing MTM drift.
                    SELECT id, symbol, direction, qty, entry_price
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
                    -- cc#123: "live" ONLY when the chosen tick is genuinely fresh
                    -- (<=10 min). Stops a prior-day bar masquerading as live at the open.
                    (lp.live_ltp IS NOT NULL AND lp.age_min IS NOT NULL AND lp.age_min <= 10) AS is_live,
                    ROUND(lp.age_min::numeric, 1)                            AS ltp_age_min,
                    lp.last_tick                                            AS last_tick
                FROM open_book h
                LEFT JOIN LATERAL (
                    -- cc#123: drive LTP from the canonical live CMP first (cmp_prices is
                    -- refreshed continuously and stays fresh even when a symbol's fyers_eq/
                    -- fyers_fut intraday bar lags the 09:15 open), else the latest intraday
                    -- tick; pick whichever is newest. Columns are IST-naive while NOW() is
                    -- UTC, so age uses NOW() AT TIME ZONE 'Asia/Kolkata'.
                    SELECT live_ltp, last_tick,
                           EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - last_tick))/60.0 AS age_min
                    FROM (
                        (SELECT cp.cmp::numeric AS live_ltp, cp.updated_at AS last_tick
                           FROM cmp_prices cp WHERE cp.symbol = h.symbol)
                        UNION ALL
                        (SELECT ip.close, ip.ts
                           FROM intraday_prices ip WHERE ip.symbol = h.symbol
                           ORDER BY ip.ts DESC LIMIT 1)
                    ) src
                    ORDER BY last_tick DESC NULLS LAST
                    LIMIT 1
                ) lp ON true
                ORDER BY h.id
            """)
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                row["entry_price"] = float(row["entry_price"]) if row["entry_price"] is not None else None
                row["ltp"]         = float(row["ltp"])         if row["ltp"]         is not None else None
                row["mtm"]         = float(row["mtm"])         if row["mtm"]         is not None else None
                row["is_live"]     = bool(row["is_live"])
                row["ltp_age_min"] = float(row["ltp_age_min"]) if row["ltp_age_min"] is not None else None
                # cc#123: last_tick = the actual live-feed tick time (IST), so the UI can
                # show real data age instead of a wall-clock that always reads "now".
                row["last_tick"]   = row["last_tick"].isoformat() if row["last_tick"] else None
                row["updated_at"]  = row["last_tick"]   # back-compat: old key now = live tick
                rows.append(row)
            # ── UNREALISED: live MTM on open positions (existing computation) ──
            unrealised   = round(sum(r["mtm"] or 0 for r in rows), 2)
            last_updated = max((r["last_tick"] for r in rows if r["last_tick"]), default=None)
            any_live     = any(r["is_live"] for r in rows)

            # ── REALISED: closed-trade P&L this week (cc_task #115) ──
            # Week = Monday-start (date_trunc('week') is ISO Monday in Postgres).
            cur.execute("""
                SELECT COALESCE(SUM(pnl), 0)
                FROM personal_journal
                WHERE result = 'CLOSED'
                  AND trade_date >= date_trunc('week', CURRENT_DATE)
            """)
            realised = round(float(cur.fetchone()[0] or 0), 2)

            # ── TOTAL: headline number = realised + unrealised ──
            total = round(realised + unrealised, 2)

            return {
                "account": "MHK40", "positions": rows,
                "realised": realised, "unrealised": unrealised, "total": total,
                "total_mtm": unrealised,  # back-compat: old field == unrealised bucket
                "position_count": len(rows), "last_updated": last_updated,
                "data_source": "live_fyers" if any_live else "holdings",
            }
    except Exception as e:
        return {"error": str(e)}


# ── SmartGain Chart — Intraday + Daily M2M timeseries ───────────────────────

@router.get("/api/smartgain/chart")
def smartgain_chart(view: str = "rolling3d"):
    """SmartGain M2M performance chart.
    view=rolling3d: 5-min M2M timeseries over the last 3 trading days (default).
    view=intraday:  today only (legacy, kept for back-compat).
    view=daily:     day-end M2M per date from smartgain_m2m snapshots.
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:

            if view == "rolling3d":
                # cc#134: last 3 trading days of 5-min bars across all open positions.
                # cc#132: open book from smartgain_holdings (broker-reconciled entry_price).
                cur.execute("""
                    WITH open_book AS (
                        SELECT symbol, direction, qty, entry_price
                        FROM smartgain_holdings
                        WHERE account = 'MHK40'
                    ),
                    trading_days AS (
                        SELECT DISTINCT ts::date AS tdate
                        FROM intraday_prices
                        ORDER BY tdate DESC
                        LIMIT 3
                    )
                    SELECT
                        ip.ts                                                       AS ts,
                        TO_CHAR(ip.ts AT TIME ZONE 'Asia/Kolkata', 'Dy HH24:MI')  AS label,
                        ROUND(SUM(
                            CASE h.direction
                                WHEN 'LONG'  THEN (ip.close - h.entry_price) * h.qty
                                WHEN 'SHORT' THEN (h.entry_price - ip.close) * h.qty
                                ELSE 0
                            END
                        )::numeric, 2)                                              AS mtm
                    FROM intraday_prices ip
                    JOIN open_book h ON h.symbol = ip.symbol
                    JOIN trading_days td ON ip.ts::date = td.tdate
                    GROUP BY ip.ts
                    ORDER BY ip.ts
                """)
                rows = cur.fetchall()
                points = [{"ts": str(r[0]), "label": r[1], "mtm": float(r[2]) if r[2] is not None else 0} for r in rows]
                return {
                    "view": "rolling3d",
                    "points": points,
                    "count": len(points),
                    "data_source": "intraday_prices (Fyers 5-min, 3 days)"
                }

            elif view == "intraday":
                # Legacy: today only (back-compat).
                cur.execute("""
                    WITH open_book AS (
                        SELECT symbol, direction, qty, entry_price
                        FROM smartgain_holdings
                        WHERE account = 'MHK40'
                    )
                    SELECT
                        ip.ts                                                       AS ts,
                        TO_CHAR(ip.ts AT TIME ZONE 'Asia/Kolkata', 'HH24:MI')      AS label,
                        ROUND(SUM(
                            CASE h.direction
                                WHEN 'LONG'  THEN (ip.close - h.entry_price) * h.qty
                                WHEN 'SHORT' THEN (h.entry_price - ip.close) * h.qty
                                ELSE 0
                            END
                        )::numeric, 2)                                              AS mtm
                    FROM intraday_prices ip
                    JOIN open_book h ON h.symbol = ip.symbol
                    WHERE ip.ts::date = CURRENT_DATE
                    GROUP BY ip.ts
                    ORDER BY ip.ts
                """)
                rows = cur.fetchall()
                points = [{"ts": str(r[0]), "label": r[1], "mtm": float(r[2]) if r[2] is not None else 0} for r in rows]
                return {
                    "view": "intraday",
                    "points": points,
                    "count": len(points),
                    "data_source": "intraday_prices (Fyers 5-min)"
                }

            else:  # daily
                # Latest snapshot per symbol per day → sum = total daily M2M
                cur.execute("""
                    SELECT
                        snapshot_date,
                        TO_CHAR(snapshot_date, 'DD Mon')   AS label,
                        ROUND(SUM(mtm)::numeric, 2)        AS day_mtm
                    FROM (
                        SELECT DISTINCT ON (symbol, snapshot_date)
                            symbol, snapshot_date, mtm
                        FROM smartgain_m2m
                        WHERE account = 'MHK40'
                        ORDER BY symbol, snapshot_date, snapshot_time DESC
                    ) latest
                    GROUP BY snapshot_date
                    ORDER BY snapshot_date
                """)
                rows = cur.fetchall()
                points = [{"date": str(r[0]), "label": r[1], "mtm": float(r[2]) if r[2] is not None else 0} for r in rows]
                return {
                    "view": "daily",
                    "points": points,
                    "count": len(points),
                    "data_source": "smartgain_m2m snapshots"
                }

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
