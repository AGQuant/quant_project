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
import os, time, httpx, re

router = APIRouter()

APP_VERSION = "max-v6-2026-06-10"   # bump every push — instant deploy verification

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TOKEN       = os.getenv("ADMIN_TOKEN", "")
BASE_URL          = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
MCP_URL = f"{BASE_URL}/mcp"

SONNET_MODEL       = "claude-sonnet-4-6"
HAIKU_MODEL        = "claude-haiku-4-5-20251001"
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
    budget_cap_usd: Optional[float] = BUDGET_CAP_DEFAULT


async def fetch_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers={"User-Agent": "Scorr/1.0"})
            r.raise_for_status()
            return r.text[:10000]
    except Exception as e:
        return f"Error fetching URL: {str(e)}"


@router.post("/api/scorr/chat")
async def scorr_chat(req: ScorrChatRequest):

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
                     "budget_cap", "native_router", "stop_button", "localStorage_persistence"],
    }
