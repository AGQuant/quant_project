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


# ─── Request Models ───────────────────────────────────────────────────────────

class ScorrQueryRequest(BaseModel):
    type: str  # top_stocks | filter | peer_compare | recommendation
    segment: Optional[str] = None
    threshold: Optional[float] = None
    limit: Optional[int] = 20
    stocks: Optional[List[str]] = None
    include_explanation: Optional[bool] = False  # Only True calls Anthropic API


# ─── Cache Helpers (0 tokens) ─────────────────────────────────────────────────

def is_cache_fresh(max_age_minutes: int = 15) -> bool:
    """Check if gvm_cache was refreshed within last 15 minutes"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_sync FROM cache_metadata WHERE key = 'gvm_cache'"
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            age_minutes = (time.time() - row[0].timestamp()) / 60
            return age_minutes < max_age_minutes
    except Exception:
        return False


def get_top_stocks_native(limit: int = 20, segment: str = None) -> list:
    """Get top N stocks by GVM from cache — 0 API tokens"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            if segment:
                cur.execute(
                    """SELECT symbol, gvm_score, growth, value, momentum, segment
                       FROM gvm_cache WHERE segment = %s AND gvm_score > 0
                       ORDER BY gvm_score DESC LIMIT %s""",
                    (segment, limit)
                )
            else:
                cur.execute(
                    """SELECT symbol, gvm_score, growth, value, momentum, segment
                       FROM gvm_cache WHERE gvm_score > 0
                       ORDER BY gvm_score DESC LIMIT %s""",
                    (limit,)
                )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


def filter_by_threshold_native(segment: str, threshold: float) -> list:
    """Filter stocks by segment + GVM threshold — 0 API tokens"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT symbol, gvm_score, growth, value, momentum, segment
                   FROM gvm_cache WHERE segment = %s AND gvm_score >= %s
                   ORDER BY gvm_score DESC""",
                (segment, threshold)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


def get_peer_comparison_native(symbol: str, segment: str) -> dict:
    """Compare stock vs peers — 0 API tokens"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT gvm_score FROM gvm_cache WHERE symbol = %s", (symbol,)
            )
            stock = cur.fetchone()
            cur.execute(
                "SELECT avg_gvm FROM peer_averages WHERE segment = %s", (segment,)
            )
            peer = cur.fetchone()

            if not stock or not peer:
                return {"symbol": symbol, "error": "not found in cache"}

            score = float(stock[0])
            peer_avg = float(peer[0])
            percentile = round((score / peer_avg) * 100, 1)

            return {
                "symbol": symbol,
                "gvm_score": score,
                "peer_avg": peer_avg,
                "percentile": percentile,
                "vs_peers": (
                    "outperforming" if percentile > 110
                    else "underperforming" if percentile < 90
                    else "in_line"
                )
            }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


# ─── Anthropic API Call (tokens used only here) ───────────────────────────────

async def call_anthropic_api(prompt: str) -> dict:
    """Call Anthropic API — only for explanations/recommendations"""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "tokens_used": 0}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",  # Cheapest model for explanations
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                }
            )
            data = response.json()

            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            # Haiku pricing: $1/$5 per million tokens
            cost_usd = (input_tokens * 1 + output_tokens * 5) / 1_000_000

            return {
                "text": text,
                "tokens_used": input_tokens + output_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
            }

    except Exception as e:
        return {"error": str(e), "tokens_used": 0}


# ─── Main Endpoint ─────────────────────────────────────────────────────────────

@router.post("/api/scorr/query")
async def scorr_query(req: ScorrQueryRequest):
    """
    Smart Scorr query endpoint.
    
    Native (0 tokens): top_stocks, filter, peer_compare
    API (~50 tokens): recommendation with include_explanation=true
    
    Examples:
      {"type": "top_stocks", "limit": 10}
      {"type": "filter", "segment": "Banks", "threshold": 7.0}
      {"type": "peer_compare", "stocks": ["HDFCBANK"], "segment": "Banks"}
      {"type": "recommendation", "stocks": ["RELIANCE"], "include_explanation": true}
    """
    start_time = time.time()
    tokens_used = 0
    api_calls = 0
    result = None

    # ── Route to native handler ─────────────────────────────────────────────
    if req.type == "top_stocks":
        result = get_top_stocks_native(req.limit or 20, req.segment)

    elif req.type == "filter":
        if not req.segment or req.threshold is None:
            return {"error": "segment and threshold required"}
        result = filter_by_threshold_native(req.segment, req.threshold)

    elif req.type == "peer_compare":
        if not req.stocks or not req.segment:
            return {"error": "stocks and segment required"}
        result = [get_peer_comparison_native(s.upper(), req.segment) for s in req.stocks]

    elif req.type == "recommendation":
        if not req.stocks:
            return {"error": "stocks required"}

        # Get scores natively first (0 tokens)
        scores = []
        with get_conn() as conn, conn.cursor() as cur:
            for symbol in req.stocks:
                cur.execute(
                    "SELECT symbol, gvm_score, growth, value, momentum, segment FROM gvm_cache WHERE symbol = %s",
                    (symbol.upper(),)
                )
                row = cur.fetchone()
                if row:
                    scores.append({
                        "symbol": row[0],
                        "gvm_score": float(row[1]),
                        "growth": float(row[2]),
                        "value": float(row[3]),
                        "momentum": float(row[4]),
                        "segment": row[5],
                    })
                else:
                    scores.append({"symbol": symbol, "error": "not in cache"})

        result = {"scores": scores}

        # Only call API if explanation requested
        if req.include_explanation:
            prompt = (
                f"Analyze these Indian stocks for retail investor recommendation:\n"
                f"{scores}\n\n"
                f"Give a brief recommendation (2-3 lines per stock). "
                f"Focus on GVM score interpretation and key risks."
            )
            api_response = await call_anthropic_api(prompt)
            result["explanation"] = api_response.get("text", "")
            tokens_used = api_response.get("tokens_used", 0)
            api_calls = 1

    else:
        return {"error": f"Unknown query type: {req.type}. Use: top_stocks, filter, peer_compare, recommendation"}

    duration_ms = round((time.time() - start_time) * 1000, 1)

    return {
        "type": req.type,
        "result": result,
        "meta": {
            "api_calls": api_calls,
            "tokens_used": tokens_used,
            "cache_used": api_calls == 0,
            "duration_ms": duration_ms,
            "cache_fresh": is_cache_fresh(),
        }
    }


@router.get("/api/scorr/health")
def scorr_health():
    """Quick health check for Scorr endpoints"""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gvm_cache")
            gvm_count = cur.fetchone()[0]
            cur.execute("SELECT last_sync, status FROM cache_metadata WHERE key = 'gvm_cache'")
            meta = cur.fetchone()

        return {
            "status": "ok",
            "gvm_cache_count": gvm_count,
            "cache_last_sync": str(meta[0]) if meta else None,
            "cache_status": meta[1] if meta else "unknown",
            "cache_fresh": is_cache_fresh(),
            "anthropic_api_key_set": bool(ANTHROPIC_API_KEY),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
