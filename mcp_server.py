# ============================================
# MCP SERVER - Project Quant
# Claude connects here to fetch live GVM data
# Deployed on Railway alongside FastAPI
# ============================================
import httpx, os
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://quantproject-production.up.railway.app"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

mcp = FastMCP("Scorr - Project Quant")

@mcp.tool()
async def get_gvm(symbol: str) -> dict:
    """Fetch full GVM score for a stock. Pass NSE symbol e.g. RELIANCE, INFY, HDFCBANK"""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/api/gvm/{symbol}", timeout=10)
        if r.status_code == 404:
            return {"error": f"{symbol} not found in GVM universe"}
        return r.json()

@mcp.tool()
async def get_top_stocks(n: int = 20, verdict: str = None) -> dict:
    """
    Get top N stocks by GVM score.
    verdict options: 'Strong Buy', 'Buy', 'Accumulate', 'Wait & Watch', 'Avoid'
    Leave verdict empty for overall top stocks.
    """
    params = {"n": n}
    if verdict:
        params["verdict"] = verdict
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/api/gvm/top", params=params, timeout=10)
        return r.json()

@mcp.tool()
async def get_sector(segment: str, n: int = 20) -> dict:
    """
    Get top stocks in a sector/segment.
    Example segments: 'IT', 'Pharma', 'Banking', 'Auto', 'FMCG', 'Defence'
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BASE_URL}/api/gvm/sector",
            params={"segment": segment, "n": n},
            timeout=10
        )
        return r.json()

@mcp.tool()
async def get_filter(
    min_score: float = 7.0,
    max_score: float = 10.0,
    verdict: str = None,
    n: int = 50
) -> dict:
    """
    Filter stocks by GVM score range.
    Default: min 7.0, max 10.0 (Buy + Strong Buy zone)
    """
    params = {"min_score": min_score, "max_score": max_score, "n": n}
    if verdict:
        params["verdict"] = verdict
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BASE_URL}/api/gvm/filter",
            params=params,
            timeout=10
        )
        return r.json()

@mcp.tool()
async def run_sql(query: str, params: list = None) -> dict:
    """
    Run any SQL query on Railway PostgreSQL.
    Use for: schema checks, data queries, migrations, analytics.
    Examples:
      - "SELECT * FROM gvm_scores WHERE symbol='RELIANCE'"
      - "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
      - "ALTER TABLE gvm_scores ADD COLUMN IF NOT EXISTS market_cap NUMERIC"
      - "SELECT segment, AVG(gvm_score) FROM gvm_scores GROUP BY segment ORDER BY AVG(gvm_score) DESC"
    Never use for: DROP TABLE, DELETE without WHERE, TRUNCATE.
    """
    import psycopg
    from psycopg.rows import dict_row

    blocked = ["drop table", "delete from", "truncate"]
    if any(b in query.lower() for b in blocked):
        return {"error": "Blocked query. DROP/DELETE/TRUNCATE not allowed via run_sql."}

    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or [])
                # DDL (ALTER, CREATE) - no rows returned
                if cur.description is None:
                    conn.commit()
                    return {"status": "ok", "message": "Query executed successfully."}
                rows = cur.fetchall()
                return {
                    "status": "ok",
                    "rows": len(rows),
                    "columns": [d.name for d in cur.description],
                    "data": [dict(r) for r in rows]
                }
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def v8_generate_overviews(tier: str = "all") -> dict:
    """
    Trigger auto-generation of input_raw overview + key_takeaway for all stocks
    using Claude API + web search. Runs as a background task on Railway.
    tier options: 'top500' (quarterly), 'longtail' (yearly), 'all' (full universe).
    Returns immediately with status=queued; generation runs server-side (~3-5 hrs for full universe).
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{BASE_URL}/api/v8/overviews/run",
            params={"tier": tier, "dry_run": False},
            timeout=15
        )
        return r.json()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
