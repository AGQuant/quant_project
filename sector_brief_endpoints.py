from fastapi import APIRouter, BackgroundTasks, Header
import psycopg, os, json, httpx, asyncio, logging

router = APIRouter()
log = logging.getLogger("scorr")

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
            );
            CREATE TABLE IF NOT EXISTS sector_themes (
                id SERIAL PRIMARY KEY,
                rank INTEGER NOT NULL,
                theme_name TEXT NOT NULL,
                tagline TEXT,
                reasoning TEXT,
                key_drivers JSONB,
                related_segments JSONB,
                macro_tailwind TEXT,
                generated_at TIMESTAMP DEFAULT NOW(),
                model TEXT DEFAULT 'claude-haiku-4-5-20251001'
            );
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
            FROM sector_ratings WHERE segment = %s
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
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        return json.loads(text.strip())

async def _save_brief(segment: str, brief: dict):
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

async def _batch_job(refresh: bool = False):
    """Background job: generate briefs for all segments missing from DB."""
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT segment FROM sector_ratings WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings) ORDER BY segment")
        all_segs = [r[0] for r in cur.fetchall()]
        if not refresh:
            cur.execute("SELECT segment FROM sector_briefs")
            done = {r[0] for r in cur.fetchall()}
            todo = [s for s in all_segs if s not in done]
        else:
            todo = all_segs

    log.info(f"[sector_brief_batch] {len(todo)} segments to generate")
    ok, fail = 0, []
    for seg in todo:
        try:
            stocks, meta = _get_context(seg)
            if not stocks:
                fail.append(f"{seg}: no stocks")
                continue
            brief = await _generate(seg, stocks, meta)
            if "error" in brief:
                fail.append(f"{seg}: {brief['error']}")
                continue
            await _save_brief(seg, brief)
            ok += 1
            log.info(f"[sector_brief_batch] ✓ {seg}")
            await asyncio.sleep(0.5)
        except Exception as e:
            fail.append(f"{seg}: {str(e)[:60]}")
            log.warning(f"[sector_brief_batch] ✗ {seg}: {e}")
    log.info(f"[sector_brief_batch] Done — {ok} ok, {len(fail)} failed")
    return ok, fail

# ── Single brief (lazy generate + cache) ─────────────────────
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
    await _save_brief(segment, brief)
    return {"segment": segment, **brief, "cached": False, "constituents": stocks}

# ── Batch generate all missing segments ──────────────────────
@router.post("/api/admin/sector/brief/batch")
async def sector_brief_batch(background_tasks: BackgroundTasks,
                             refresh: bool = False,
                             x_admin_token: str = Header(None)):
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT segment) FROM sector_ratings WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sector_briefs")
        cached = cur.fetchone()[0]
    background_tasks.add_task(_batch_job, refresh)
    return {"status": "started", "total_segments": total, "already_cached": cached,
            "to_generate": total - cached if not refresh else total,
            "note": "Check /api/admin/sector/brief/status for progress"}

# ── Status check ─────────────────────────────────────────────
@router.get("/api/admin/sector/brief/status")
def sector_brief_status():
    _ensure_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT segment) FROM sector_ratings WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), MAX(generated_at)::text FROM sector_briefs")
        r = cur.fetchone(); cached, last_gen = r[0], r[1]
        cur.execute("SELECT segment, generated_at::text FROM sector_briefs ORDER BY generated_at DESC LIMIT 5")
        recent = [{"segment": r[0], "at": r[1]} for r in cur.fetchall()]
    return {"total_segments": total, "briefs_cached": cached,
            "missing": total - cached, "pct_complete": round(cached/total*100, 1) if total else 0,
            "last_generated": last_gen, "recent": recent}

# ── Emerging Themes ───────────────────────────────────────────
@router.get("/api/sector/themes")
def sector_themes():
    """Return 10 emerging themes with top companies per theme from live GVM data."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            # Fetch all themes
            cur.execute("""
                SELECT rank, theme_name, tagline, reasoning,
                       key_drivers, related_segments, macro_tailwind,
                       generated_at::text
                FROM sector_themes
                ORDER BY rank
            """)
            cols = [d[0] for d in cur.description]
            themes = [dict(zip(cols, r)) for r in cur.fetchall()]

            # For each theme, fetch top 10 companies across related segments
            score_date_q = "SELECT MAX(score_date) FROM gvm_scores"
            for t in themes:
                segs = t.get("related_segments") or []
                if not segs:
                    t["companies"] = []
                    continue
                cur.execute("""
                    SELECT g.symbol, g.company_name,
                           ROUND(g.gvm_score::numeric, 2) AS gvm_score,
                           g.segment, g.verdict,
                           ROUND(g.g_score::numeric, 2) AS g_score,
                           ROUND(g.v_score::numeric, 2) AS v_score,
                           ROUND(g.m_score::numeric, 2) AS m_score
                    FROM gvm_scores g
                    WHERE g.segment = ANY(%s)
                      AND g.score_date = (""" + score_date_q + """)
                    ORDER BY g.gvm_score DESC
                    LIMIT 10
                """, (segs,))
                ccols = [d[0] for d in cur.description]
                t["companies"] = [dict(zip(ccols, r)) for r in cur.fetchall()]

        return {"count": len(themes), "themes": themes}
    except Exception as e:
        return {"error": str(e)}
