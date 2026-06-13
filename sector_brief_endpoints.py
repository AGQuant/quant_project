from fastapi import APIRouter
import psycopg, os, json, httpx

router = APIRouter()

def get_conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def _ensure_table():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sector_briefs (
                id SERIAL PRIMARY KEY,
                segment TEXT NOT NULL UNIQUE,
                what_is_it TEXT,
                growth_drivers JSONB,
                application_type TEXT,
                business_model TEXT,
                key_risks JSONB,
                generated_at TIMESTAMP DEFAULT NOW(),
                model TEXT DEFAULT 'claude-haiku-4-5-20251001'
            )
        """)
        conn.commit()

def _get_context(segment: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT g.symbol, g.company_name,
                   ROUND(g.gvm_score::numeric,2) AS gvm_score,
                   g.verdict, ROUND(g.g_score::numeric,2) AS g_score,
                   ROUND(g.v_score::numeric,2) AS v_score,
                   ROUND(g.m_score::numeric,2) AS m_score,
                   ROUND(g.market_cap::numeric,0) AS market_cap
            FROM gvm_scores g
            WHERE g.segment = %s
              AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
            ORDER BY g.gvm_score DESC
        """, (segment,))
        cols = [d[0] for d in cur.description]
        stocks = [dict(zip(cols, r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT mcap_weighted_gvm, verdict, stocks_count, total_mcap
            FROM sector_ratings
            WHERE segment = %s
              AND score_date = (SELECT MAX(score_date) FROM sector_ratings)
        """, (segment,))
        r = cur.fetchone()
        meta = {"gvm": float(r[0]) if r else None, "verdict": r[1] if r else None,
                "stocks_count": r[2] if r else None, "total_mcap": float(r[3]) if r else None} if r else {}
    return stocks, meta

async def _generate(segment: str, stocks: list, meta: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}

    top5 = ", ".join([f"{s['symbol']} (GVM {s['gvm_score']})" for s in stocks[:5]])
    prompt = f"""You are writing an investor education brief for Indian retail investors.

Sector: {segment}
Top stocks: {top5}
Sector GVM: {meta.get('gvm','N/A')} | Verdict: {meta.get('verdict','N/A')} | {meta.get('stocks_count','N/A')} companies | ₹{meta.get('total_mcap','N/A')}L Cr market cap

Return ONLY valid JSON with these exact keys (no markdown, no extra text):
{{
  "what_is_it": "2 sentences: what companies in this sector do + their role in the Indian economy",
  "growth_drivers": ["driver 1 (concise, 8-15 words)", "driver 2", "driver 3", "driver 4"],
  "application_type": "Tags like: B2B / B2C / Government / Cyclical / Defensive / Export-led / Domestic / Capital-intensive",
  "business_model": "One sentence on how these companies primarily earn revenue",
  "key_risks": ["risk 1 (concise, 8-15 words)", "risk 2", "risk 3"]
}}"""

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]}
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        # Strip any accidental markdown fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())

@router.get("/api/sector/brief")
async def sector_brief(segment: str, refresh: bool = False):
    _ensure_table()

    if not refresh:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT what_is_it, growth_drivers, application_type,
                       business_model, key_risks, generated_at::text
                FROM sector_briefs WHERE segment = %s
            """, (segment,))
            row = cur.fetchone()
            if row:
                stocks, _ = _get_context(segment)
                return {"segment": segment, "what_is_it": row[0], "growth_drivers": row[1],
                        "application_type": row[2], "business_model": row[3],
                        "key_risks": row[4], "generated_at": row[5],
                        "cached": True, "constituents": stocks}

    stocks, meta = _get_context(segment)
    if not stocks:
        return {"error": f"Segment '{segment}' not found"}

    brief = await _generate(segment, stocks, meta)
    if "error" in brief:
        return brief

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sector_briefs (segment, what_is_it, growth_drivers,
                application_type, business_model, key_risks)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (segment) DO UPDATE SET
                what_is_it = EXCLUDED.what_is_it,
                growth_drivers = EXCLUDED.growth_drivers,
                application_type = EXCLUDED.application_type,
                business_model = EXCLUDED.business_model,
                key_risks = EXCLUDED.key_risks,
                generated_at = NOW()
        """, (segment, brief["what_is_it"], json.dumps(brief["growth_drivers"]),
              brief["application_type"], brief["business_model"], json.dumps(brief["key_risks"])))
        conn.commit()

    return {"segment": segment, **brief, "cached": False, "constituents": stocks}
