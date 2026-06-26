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


# ── Request Models ──────────────────────────────────────────────────────────

class ScorrQueryRequest(BaseModel):
    type: str
    segment: Optional[str] = None
    threshold: Optional[float] = None
    limit: Optional[int] = 20
    stocks: Optional[List[str]] = None
    include_explanation: Optional[bool] = False


# ── Cache Helpers ────────────────────────────────────────────────────────────

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


# ── Anthropic API Call ───────────────────────────────────────────────────────

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


# ── Main Endpoint ────────────────────────────────────────────────────────────

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
    """SmartGain MHK40 holdings — M2M with live LTP from Fyers intraday feed."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                    h.symbol, h.direction, h.qty,
                    ROUND(h.entry_price::numeric, 2)                         AS entry_price,
                    ROUND(COALESCE(lp.live_ltp, h.ltp)::numeric, 2)         AS ltp,
                    ROUND(
                        CASE h.direction
                            WHEN 'LONG'  THEN (COALESCE(lp.live_ltp, h.ltp) - h.entry_price) * h.qty
                            WHEN 'SHORT' THEN (h.entry_price - COALESCE(lp.live_ltp, h.ltp)) * h.qty
                            ELSE 0
                        END::numeric, 2
                    )                                                         AS mtm,
                    (lp.live_ltp IS NOT NULL)                                AS is_live,
                    GREATEST(h.updated_at, lp.last_tick)                    AS updated_at
                FROM smartgain_holdings h
                LEFT JOIN LATERAL (
                    SELECT close AS live_ltp, ts AS last_tick
                    FROM intraday_prices
                    WHERE symbol = h.symbol
                    ORDER BY ts DESC LIMIT 1
                ) lp ON true
                WHERE h.account = 'MHK40'
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
                row["updated_at"]  = row["updated_at"].isoformat() if row["updated_at"] else None
                rows.append(row)
            total_mtm    = round(sum(r["mtm"] or 0 for r in rows), 2)
            last_updated = max((r["updated_at"] for r in rows if r["updated_at"]), default=None)
            any_live     = any(r["is_live"] for r in rows)
            return {
                "account": "MHK40", "positions": rows, "total_mtm": total_mtm,
                "position_count": len(rows), "last_updated": last_updated,
                "data_source": "live_fyers" if any_live else "manual_screenshot",
            }
    except Exception as e:
        return {"error": str(e)}


# ── SmartGain Chart — Intraday + Daily M2M timeseries ───────────────────────

@router.get("/api/smartgain/chart")
def smartgain_chart(view: str = "intraday"):
    """SmartGain M2M performance chart.
    view=intraday: 5-min M2M timeseries for today from Fyers intraday_prices.
    view=daily:    Day-end M2M per date from smartgain_m2m snapshots.
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:

            if view == "intraday":
                # Compute total M2M at each 5-min bar today across all open positions
                cur.execute("""
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
                    JOIN smartgain_holdings h ON h.symbol = ip.symbol AND h.account = 'MHK40'
                    WHERE ip.ts::date = CURRENT_DATE
                    GROUP BY ip.ts
                    ORDER BY ip.ts
                """)
                rows = cur.fetchall()
                points = [{"ts": str(r[0]), "label": r[1], "mtm": float(r[2]) if r[2] is not None else 0} for r in rows]
                return {
                    "view": "intraday",
                    "date": str(__import__("datetime").date.today()),
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
