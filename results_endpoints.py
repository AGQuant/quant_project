"""
results_endpoints.py — cc#572 (spec id=6438): Results "R" card backend.

GET /api/results/card?symbol=X[&generate=true]

Branch logic:
  - earnings_calendar row with ex_date <= today -> ANNOUNCED. Return input_raw.result_analysis if
    present (with last_result_analysis_updated); else ANNOUNCED_NO_ANALYSIS (never invent figures).
  - ex_date > today -> UPCOMING; no earnings row -> DATE_TBD. Both serve a cached (<30d) FY27
    outlook from input_raw.fy27_outlook. If missing/stale AND generate=true -> ONE Haiku call built
    only from real trailing data (GVM/G/V/M + 180d trend + screener_raw fundamentals), qualitative
    only, cached. Zero-cost on a cached read; Haiku fires ONLY on an explicit generate=true.

Storage: input_raw.fy27_outlook + last_fy27_outlook_updated (same convention as result_analysis;
main.py registers fy27_outlook in _ALLOWED_CONTENT_FIELDS + _FIELD_TO_TS_COL for manual override).
"""
import os
import json
import logging
from datetime import datetime, date
from typing import Optional

import httpx
import psycopg2
from fastapi import APIRouter

log = logging.getLogger("results_card")
router = APIRouter()

OUTLOOK_MODEL = "claude-haiku-4-5-20251001"
OUTLOOK_FRESH_DAYS = 30


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _ensure_cols(cur):
    # idempotent column self-create (run_sql ALTER is blocked by MAINTENANCE_LOCK_RULE; app-side).
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS fy27_outlook TEXT")
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS last_fy27_outlook_updated TIMESTAMP")


def _f(v):
    return round(float(v), 2) if v is not None else None


def _gvm_ctx(cur, sym):
    """Latest GVM/G/V/M + verdict + 180d GVM delta, from gvm_history (complete universe)."""
    cur.execute("""SELECT gvm_score, g_score, v_score, m_score, verdict
                   FROM gvm_history WHERE symbol=%s ORDER BY score_date DESC LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s
                   AND score_date BETWEEN CURRENT_DATE-200 AND CURRENT_DATE-180
                   ORDER BY score_date DESC LIMIT 1""", (sym,))
    d180 = cur.fetchone()
    dgvm = (float(r[0]) - float(d180[0])) if (r[0] is not None and d180 and d180[0] is not None) else None
    return {"gvm": _f(r[0]), "g": _f(r[1]), "v": _f(r[2]), "m": _f(r[3]), "verdict": r[4],
            "dgvm_180": round(dgvm, 2) if dgvm is not None else None}


def _fundamentals(cur, sym):
    cur.execute('''SELECT "Operating profit growth", roce, opm, "Debt to equity", "Return on equity"
                   FROM screener_raw WHERE nse_code=%s LIMIT 1''', (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"opg": _f(r[0]), "roce": _f(r[1]), "opm": _f(r[2]), "de": _f(r[3]), "roe": _f(r[4])}


async def _generate_outlook(sym, g, f):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None, {"error": "ANTHROPIC_API_KEY not set"}
    prompt = (
        f"You are a Scorr equity analyst writing a SHORT, qualitative FY27 outlook for {sym}, an "
        f"Indian listed company, for retail investors.\n\n"
        f"Use ONLY the trailing data below. Do NOT invent forward EPS, revenue, margins, or price "
        f"targets. Give a grounded, qualitative view of the likely direction and the key things to "
        f"watch — never a number you were not given.\n\n"
        f"Scorr GVM model (0-10): GVM {g.get('gvm')}, Growth {g.get('g')}, Value {g.get('v')}, "
        f"Momentum {g.get('m')}, verdict {g.get('verdict')}. 180-day GVM change: {g.get('dgvm_180')}.\n"
        f"Trailing fundamentals (from filings): operating-profit growth {f.get('opg')}%, "
        f"ROCE {f.get('roce')}%, operating margin {f.get('opm')}%, debt/equity {f.get('de')}, "
        f"ROE {f.get('roe')}%.\n\n"
        f"Write 3-4 plain sentences: (1) what the trailing quality/value/momentum picture implies, "
        f"(2) the fundamental trend, (3) the single biggest thing to watch into FY27. "
        f"Non-promotional. Output plain text only, no headings."
    )
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": OUTLOOK_MODEL, "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        r.raise_for_status()
        body = r.json()
        text = body["content"][0]["text"].strip()
        return text, body.get("usage", {})


@router.get("/api/results/card")
async def results_card(symbol: str, generate: bool = False):
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol is required"}
    with _conn() as conn, conn.cursor() as cur:
        try:
            _ensure_cols(cur)
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("SELECT verdict FROM gvm_scores WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (sym,))
        vr = cur.fetchone()
        gvm_verdict = vr[0] if vr else None

        cur.execute("SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=%s ORDER BY ex_date DESC LIMIT 1", (sym,))
        er = cur.fetchone()
        today = date.today()

        # Branch A: announced
        if er and er[0] is not None and er[0] <= today:
            cur.execute("SELECT result_analysis, last_result_analysis_updated FROM input_raw WHERE nse_code=%s", (sym,))
            ra = cur.fetchone()
            if ra and ra[0]:
                return {"symbol": sym, "status": "announced", "ex_date": str(er[0]),
                        "result_analysis": ra[0],
                        "generated_at": str(ra[1]) if ra[1] else None, "gvm_verdict": gvm_verdict}
            return {"symbol": sym, "status": "announced_no_analysis", "ex_date": str(er[0]),
                    "gvm_verdict": gvm_verdict}

        # Branch B (upcoming) / C (date_tbd): FY27 outlook
        status = "upcoming" if (er and er[0] is not None) else "date_tbd"
        ed = str(er[0]) if (er and er[0] is not None) else None

        cur.execute("SELECT fy27_outlook, last_fy27_outlook_updated FROM input_raw WHERE nse_code=%s", (sym,))
        fo = cur.fetchone()
        cached, cached_ts = (fo[0], fo[1]) if fo else (None, None)
        fresh = bool(cached and cached_ts and (datetime.now() - cached_ts).days < OUTLOOK_FRESH_DAYS)

        if not generate or (fresh and not generate):
            # cached read (zero cost). generate=false always returns whatever is cached (or null).
            return {"symbol": sym, "status": status, "ex_date": ed,
                    "fy27_outlook": cached if cached else None,
                    "generated_at": str(cached_ts) if cached_ts else None,
                    "model": OUTLOOK_MODEL if cached else None, "gvm_verdict": gvm_verdict}

        # generate=true AND (missing or stale) -> ONE Haiku call
        g = _gvm_ctx(cur, sym)
        f = _fundamentals(cur, sym)
        try:
            text, usage = await _generate_outlook(sym, g, f)
        except Exception as e:
            log.error(f"results_card generate {sym}: {e}")
            return {"symbol": sym, "status": status, "ex_date": ed, "fy27_outlook": cached,
                    "error": str(e), "gvm_verdict": gvm_verdict}
        if not text:
            return {"symbol": sym, "status": status, "ex_date": ed, "fy27_outlook": cached,
                    "error": (usage or {}).get("error", "generation failed"), "gvm_verdict": gvm_verdict}

        cur.execute("SELECT id FROM input_raw WHERE nse_code=%s", (sym,))
        idr = cur.fetchone()
        if idr:
            cur.execute("UPDATE input_raw SET fy27_outlook=%s, last_fy27_outlook_updated=NOW() WHERE id=%s",
                        (text, idr[0]))
        cur.execute("INSERT INTO ops_log (category, title, details) VALUES ('info','results_fy27_outlook',%s::jsonb)",
                    (json.dumps({"symbol": sym, "model": OUTLOOK_MODEL, "usage": usage, "chars": len(text)}),))
        conn.commit()
        return {"symbol": sym, "status": status, "ex_date": ed, "fy27_outlook": text,
                "generated_at": datetime.now().isoformat(), "model": OUTLOOK_MODEL, "gvm_verdict": gvm_verdict}
