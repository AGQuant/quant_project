"""
Scorr Chat Endpoint — CIO Assistant Max with full Railway tool access.

POST /api/scorr/chat
  - claude_on=false  → pure Railway native via native_router ($0)
  - claude_on=true   → Sonnet default, hard cap $0.05/query

GET /api/scorr/chat/health → includes app_version for deploy verification
"""

from fastapi import APIRouter, HTTPException
from native_router import route_native
from pydantic import BaseModel
from typing import Optional, List, Any
import os, time, httpx, re, asyncio
import psycopg

router = APIRouter()

APP_VERSION = "max-v7-2026-06-28"   # bump every push — instant deploy verification (cc#108 mode selector)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TOKEN       = os.getenv("ADMIN_TOKEN", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
DATABASE_URL      = os.getenv("DATABASE_URL", "")
BASE_URL          = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
MCP_URL = f"{BASE_URL}/mcp"

SONNET_MODEL       = "claude-sonnet-4-6"
HAIKU_MODEL        = "claude-haiku-4-5-20251001"
GEMINI_MODEL       = "gemini-2.5-flash-lite"
DEFAULT_MODEL      = SONNET_MODEL
DEFAULT_MAX_TOKENS = 3000
HARD_MAX_TOKENS    = 4096
BUDGET_CAP_DEFAULT = 0.05

PRICING = {
    HAIKU_MODEL:       (1.0,  5.0),
    SONNET_MODEL:      (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
}

def tokens_from_budget(budget_usd: float, model: str) -> int:
    _, out_price = PRICING.get(model, (3.0, 15.0))
    return min(int(budget_usd / (out_price / 1_000_000)), HARD_MAX_TOKENS)

SYSTEM_PROMPT = """You are Max — Arpit's AI Chief Investment Officer (CIO Assistant Max) built on Scorr.
You have full access to all Railway MCP tools: run_sql, V8 signals, GVM scores, QB positions, paper trades, github_push, session_log, and more.

IDENTITY & ROLE
- Personal CIO for Arpit Goel. Platform: scorr.in. Mission: Freedom by 2035, Rs.500 Cr floor.
- You are the daily ops brain. Claude.ai (separate) handles strategy and specs.
- Railway PostgreSQL = single source of truth. Never hardcode. Never assume — query first.

MEMORY FRAMEWORK (5 layers — read on resume)
- Layer 0: memory_rules id=156 — how memory works
- Layer 1: debug_learnings id=207 — pre-code checklist, mistakes never to repeat
- Layer 2: spec_registry id=150 — which spec is current
- Layer 3: day_log/week_log — singletons
- Layer 4: task entries by category
- IGNORE category=archived_superseded

CAPABILITIES
- IMAGE PASTE + FILE UPLOAD: images (PNG/JPG/GIF), PDFs, text files.
- URL FETCH: read any HTTP(S) URL pasted in chat.
- MODEL: Sonnet default. $0.05 hard cap/query. Claude OFF = native_router ($0).

COMMUNICATION RULES (NON-NEGOTIABLE)
- SHORT answers first. No preambles. Tables for data.
- Detail only when explicitly asked.
- All times IST. NSE Mon–Fri 09:15–15:30.

TRADING RULES
- V8 gate closed = no entries. Blackout enforced. 1 lot. 15:20 IST entry cutoff.
- Price action over fundamentals. GVM = quality gate (LONG only — NOT applicable for SHORT).
- Tier 1 (12 rules, min 8/12) → Tier 2 (6 filters, min 4/6). Side-aware: SHORT inverts sector/RSI/week-month rules.
- Fibonacci: EOD raw_prices 60-day swing high/low. SHORT = bounce to fib resistance + rejection.
- v8_journal (algo) and personal_journal (manual) NEVER mixed.

GITHUB / DEPLOY
- ALWAYS ask before any github_push. Batch all changes — ONE push per sprint.
- ast.parse() before push. Railway auto-deploy ~90s.

MEMORY WRITES
- run_sql to read/write session_log. 4-place protocol on 'update Railway'.
"""


class ChatMessage(BaseModel):
    role: str
    content: Any

class ScorrChatRequest(BaseModel):
    messages:       List[ChatMessage]
    model:          Optional[str]   = DEFAULT_MODEL
    max_tokens:     Optional[int]   = DEFAULT_MAX_TOKENS
    use_tools:      Optional[bool]  = True
    claude_on:      Optional[bool]  = True
    mode:           Optional[str]   = None   # cc#108: native | haiku | gemini
    budget_cap_usd: Optional[float] = BUDGET_CAP_DEFAULT


async def fetch_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers={"User-Agent": "Scorr/1.0"})
            r.raise_for_status()
            return r.text[:10000]
    except Exception as e:
        return f"Error fetching URL: {str(e)}"


# ── cc#108: lightweight 3-mode selector (native | haiku | gemini) ──────────────
LIGHT_SYSTEM = (
    "You are Max, Arpit\'s AI Chief Investment Officer on Scorr. Answer using the DB "
    "CONTEXT provided. Be concise, precise, analytical; use tables for data; all times IST. "
    "Never fabricate numbers not in the context. If the context has no matching rows, say so "
    "and answer from general knowledge only if clearly flagged."
)


def _q(sql, params=()):
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fmt(rows, title):
    if not rows:
        return ""
    out = [title + ":"]
    for r in rows:
        out.append("  - " + ", ".join(f"{k}={v}" for k, v in r.items() if v is not None))
    return "\n".join(out)


def _db_context(query):
    """Keyword-driven DB prefetch (mirrors /test-cio) -> plain-text context block."""
    ql = (query or "").lower()
    blocks = []
    toks = re.findall(r"[A-Za-z]{3,}", query or "")
    stop = {"the", "what", "show", "today", "todays", "which", "score", "gvm", "stock", "stocks",
            "give", "tell", "about", "for", "and", "with", "sector", "strongest", "signals",
            "market", "mood", "bank", "max", "cio", "give", "list"}
    cand = [t for t in toks if t.lower() not in stop]
    if cand:
        try:
            rows = _q("""SELECT symbol, company_name, gvm_score, verdict, g_score, v_score, m_score, segment
                         FROM gvm_scores
                         WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                           AND (UPPER(symbol)=ANY(%s) OR company_name ILIKE ANY(%s))
                         ORDER BY gvm_score DESC NULLS LAST LIMIT 5""",
                      ([t.upper() for t in cand], [f"%{t}%" for t in cand]))
            b = _fmt(rows, "GVM data")
            if b:
                blocks.append(b)
        except Exception:
            pass
    if any(k in ql for k in ("v8", "signal", "trade", "qualified", "today")):
        try:
            rows = _q("""SELECT symbol, basket, cmp, gvm_score, signal_ts FROM v8_qualified
                         WHERE signal_date=CURRENT_DATE ORDER BY gvm_score DESC NULLS LAST LIMIT 10""")
            b = _fmt(rows, "V8 qualified signals today")
            if b:
                blocks.append(b)
        except Exception:
            pass
    if any(k in ql for k in ("sector", "strongest", "rotation", "industry")):
        try:
            rows = _q("""SELECT segment, mcap_weighted_gvm, verdict FROM sector_ratings
                         WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)
                         ORDER BY mcap_weighted_gvm DESC NULLS LAST LIMIT 10""")
            b = _fmt(rows, "Sector ratings (mcap-weighted GVM)")
            if b:
                blocks.append(b)
        except Exception:
            pass
    return "\n\n".join(blocks)


async def _call_haiku(system, user):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": HAIKU_MODEL, "max_tokens": 1200, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        d = r.json()
    text = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    return text, u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0


async def _call_gemini(system, user, grounded=False):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}}
    if grounded:
        body["tools"] = [{"google_search": {}}]
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=body)
        r.raise_for_status()
        d = r.json()
    text = ""
    cand = d.get("candidates") or []
    if cand:
        parts = (cand[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
    um = d.get("usageMetadata") or {}
    return text, um.get("promptTokenCount", 0) or 0, um.get("candidatesTokenCount", 0) or 0


async def _mode_route(mode, query_text):
    t0 = time.time()
    if mode == "native":
        reply = await route_native(query_text)
        return {"reply": reply, "tool_calls": [], "model": None, "stop_reason": "native",
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                "duration_ms": round((time.time() - t0) * 1000, 1)}

    context = await asyncio.to_thread(_db_context, query_text)
    has_ctx = bool(context.strip())
    user = (f"DB CONTEXT:\n{context}\n\n" if has_ctx else "DB CONTEXT: (no matching rows)\n\n") + f"USER QUESTION: {query_text}"
    model_used = None
    try:
        if mode == "haiku":
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            reply, tin, tout = await _call_haiku(LIGHT_SYSTEM, user)
            cost = round((tin * 1.0 + tout * 5.0) / 1_000_000, 6)
            model_used = HAIKU_MODEL
        else:  # gemini
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY not set")
            reply, tin, tout = await _call_gemini(LIGHT_SYSTEM, user, grounded=not has_ctx)
            cost = round((tin * 0.1 + tout * 0.4) / 1_000_000, 6)
            model_used = GEMINI_MODEL
    except Exception as e:
        # cc#108: if gemini fails, silently fall back to haiku (UI keeps Gemini label)
        if mode == "gemini" and ANTHROPIC_API_KEY:
            try:
                reply, tin, tout = await _call_haiku(LIGHT_SYSTEM, user)
                cost = round((tin * 1.0 + tout * 5.0) / 1_000_000, 6)
                model_used = HAIKU_MODEL + " (gemini-fallback)"
            except Exception as e2:
                return {"reply": f"Error ({mode}): {e2}", "tool_calls": [], "model": None,
                        "stop_reason": "error", "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                        "duration_ms": round((time.time() - t0) * 1000, 1)}
        else:
            return {"reply": f"Error ({mode}): {e}", "tool_calls": [], "model": None,
                    "stop_reason": "error", "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                    "duration_ms": round((time.time() - t0) * 1000, 1)}
    return {"reply": reply, "tool_calls": [], "model": model_used, "stop_reason": mode,
            "usage": {"input_tokens": tin, "output_tokens": tout, "cost_usd": cost},
            "duration_ms": round((time.time() - t0) * 1000, 1)}


@router.post("/api/scorr/chat")
async def scorr_chat(req: ScorrChatRequest):

    # ── cc#108: 3-mode selector — native | haiku | gemini ──
    _mode = (req.mode or "").strip().lower()
    if _mode in ("native", "haiku", "gemini"):
        _last = req.messages[-1].content if req.messages else ""
        _qt = _last if isinstance(_last, str) else str(_last)
        return await _mode_route(_mode, _qt)

    # ── Native path: Claude OFF — real DB query, zero tokens ──────
    if not req.claude_on:
        t0 = time.time()
        last_msg = req.messages[-1].content if req.messages else ""
        query_text = last_msg if isinstance(last_msg, str) else str(last_msg)
        reply = await route_native(query_text)
        return {
            "reply": reply,
            "tool_calls": [],
            "model": None,
            "stop_reason": "native",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            "duration_ms": round((time.time() - t0) * 1000, 1),
        }

    # ── Claude ON path ─────────────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set in Railway env")

    model         = req.model or DEFAULT_MODEL
    budget_cap    = req.budget_cap_usd or BUDGET_CAP_DEFAULT
    token_ceiling = tokens_from_budget(budget_cap, model)
    max_tokens    = min(req.max_tokens or DEFAULT_MAX_TOKENS, token_ceiling, HARD_MAX_TOKENS)

    messages = []
    for m in req.messages:
        msg_dict = {"role": m.role}
        if isinstance(m.content, str):
            urls = re.findall(r'https?://[^\s]+', m.content)
            if urls and m.role == "user":
                fetched = m.content
                for url in urls:
                    url_data = await fetch_url(url)
                    fetched += f"\n\n[URL Content from {url}]:\n{url_data}"
                msg_dict["content"] = fetched
            else:
                msg_dict["content"] = m.content
        else:
            msg_dict["content"] = m.content
        messages.append(msg_dict)

    payload = {
        "model": model, "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT, "messages": messages,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
        "content-type": "application/json", "anthropic-beta": "mcp-client-2025-04-04",
    }
    if req.use_tools:
        payload["mcp_servers"] = [{
            "type": "url", "url": MCP_URL, "name": "scorr",
            "authorization_token": ADMIN_TOKEN,
        }]

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Anthropic call failed: {e}")

    if "error" in data:
        raise HTTPException(502, f"API error: {data['error'].get('message', data['error'])}")

    text_parts, tool_calls = [], []
    for block in data.get("content", []):
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "mcp_tool_use":
            tool_calls.append({"name": block.get("name"), "input": block.get("input")})

    usage   = data.get("usage", {}) or {}
    in_tok  = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    ir, or_ = PRICING.get(model, (3.0, 15.0))
    cost    = round((in_tok * ir + out_tok * or_) / 1_000_000, 6)

    return {
        "reply":       "\n".join(text_parts).strip(),
        "tool_calls":  tool_calls,
        "model":       model,
        "stop_reason": data.get("stop_reason"),
        "usage":       {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost},
        "duration_ms": round((time.time() - start) * 1000, 1),
    }


@router.get("/api/scorr/chat/health")
def scorr_chat_health():
    return {
        "status":                "ok",
        "app_version":           APP_VERSION,
        "anthropic_api_key_set": bool(ANTHROPIC_API_KEY),
        "admin_token_set":       bool(ADMIN_TOKEN),
        "mcp_url":               MCP_URL,
        "default_model":         DEFAULT_MODEL,
        "budget_cap_usd":        BUDGET_CAP_DEFAULT,
        "max_tokens_default":    DEFAULT_MAX_TOKENS,
        "features": ["image_paste", "file_upload", "url_fetch", "claude_toggle",
                     "budget_cap", "native_router", "stop_button", "localStorage_persistence", "mode_selector"],
    }
