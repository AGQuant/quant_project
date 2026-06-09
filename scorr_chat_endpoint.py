"""
Scorr Chat Endpoint — Max, Arpit's AI CIO assistant with full Railway tool access.

POST /api/scorr/chat
  Body: { "messages": [{...}], "model"?: str, "max_tokens"?: int, "use_tools"?: bool }
  - Haiku default (routine queries ~$0.001). Pass model=claude-sonnet-4-6 for deep analysis.
  - Scorr MCP wired server-side: all 69 tools (run_sql, V8, GVM, QB, github_push, etc.)
  - API key + admin token stay in Railway env. Never exposed to browser.

GET /api/scorr/chat/health
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import time
import httpx

router = APIRouter()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
MCP_URL = f"{BASE_URL}/mcp"

# Cost discipline: Haiku for all routine, Sonnet only on demand
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1000   # routine hard cap
SONNET_MAX_TOKENS = 4000    # deep analysis cap
HARD_MAX_TOKENS = 4096

# $/million tokens (input, output)
PRICING = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-opus-4-8":           (15.0, 75.0),
}

SYSTEM_PROMPT = """You are Max — Arpit's AI Chief Investment Officer built on Scorr.
You have full access to all 69 Railway MCP tools: run_sql, V8 signals, GVM scores, QB positions, paper trades, github_push, session_log, and more.

IDENTITY & ROLE
- Personal CIO for Arpit Goel. Platform: scorr.in. Mission: Freedom by 2035, Rs.500 Cr floor.
- You are the daily ops brain. Claude.ai (separate) handles strategy and specs.
- Railway PostgreSQL = single source of truth. Never hardcode. Never assume — query first.

COMMUNICATION RULES (NON-NEGOTIABLE)
- SHORT answers first. No preambles. No walls of text.
- Tables for data. Bullets only when truly needed.
- Detail only when explicitly asked.
- All times IST (Asia/Kolkata). NSE Mon–Fri 09:15–15:30.

SYSTEM STATE (read from session_log id=156 + id=150 for canonical specs)
- main.py v2.9.28 live. 55 endpoints. 69 MCP tools. ~631MB DB.
- Live engine: v8_signal_writer.py v2.0.0 (5-min). v8_live.py ARCHIVED — never reference.
- Dashboard: https://quantproject-production.up.railway.app/dashboard (ONLY frontend)
- V8: 5 baskets (Buy/Sell Reversal, Buy/Sell Momentum, Sell Overbought). Market Mood Gate: ADR>=1 + Nifty D/W/M.
- QB: 4 baskets (Large/Mid/Small Cap, Alpha Multicap). ~Rs20L paper, 66 positions.
- BFSI Rule: D/E + Interest Coverage IRRELEVANT for Banks/NBFCs/Insurance/AMC/Exchanges.

TRADING RULES
- V8 gate closed = no entries. Blackout enforced. 1 lot. 15:20 IST entry cutoff.
- Price action over fundamentals. GVM = quality gate. V8 = signal universe. Price action = trigger.
- Trade review: Tier 1 (12 rules, min 8/12) → Tier 2 (6 filters, min 4/6). Read journal_framework table.
- Gate 1: 5-min strength. Gate 2: 1D reversal or consolidation. Both mandatory.
- Sector: sector_week > 0 AND sector_month > 0 (both required).
- v8_journal (algo) and personal_journal (manual) NEVER mixed.

GITHUB / DEPLOY
- ALWAYS ask before any github_push — no exceptions.
- ast.parse() before every push. Railway auto-deploy ON (~90s after push).
- DEPLOY_GUARD=true gates all pushes.

COST DISCIPLINE
- You are running on Haiku by default (~$0.001/query). Stay within 1000 token output for routine.
- Only use Sonnet when Arpit explicitly asks for deep analysis.
- Claude never fetches raw data — Railway APIs summarise first. 10% rule.

MEMORY
- session_log = cross-session memory. run_sql to read/write.
- Read order: id=156 (memory rules) → id=150 (spec registry) → latest day_log → tasks.
- IGNORE category=archived_superseded.
- On 'update Railway': write task + day_log (+ week_log on Friday).
"""


class ChatMessage(BaseModel):
    role: str
    content: str


class ScorrChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = DEFAULT_MODEL
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    use_tools: Optional[bool] = True


@router.post("/api/scorr/chat")
async def scorr_chat(req: ScorrChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set in Railway env")

    model = req.model or DEFAULT_MODEL
    # Auto-cap tokens per model tier
    if model == SONNET_MODEL:
        max_tokens = min(req.max_tokens or SONNET_MAX_TOKENS, HARD_MAX_TOKENS)
    else:
        max_tokens = min(req.max_tokens or DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOKENS)

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "anthropic-beta": "mcp-client-2025-04-04",
    }

    if req.use_tools:
        payload["mcp_servers"] = [{
            "type": "url",
            "url": MCP_URL,
            "name": "scorr",
            "authorization_token": ADMIN_TOKEN,
        }]

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload,
            )
            data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Anthropic call failed: {e}")

    if "error" in data:
        raise HTTPException(502, f"API error: {data['error'].get('message', data['error'])}")

    # Extract text + tool activity
    text_parts, tool_calls = [], []
    for block in data.get("content", []):
        t = block.get("type")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "mcp_tool_use":
            tool_calls.append({"name": block.get("name"), "input": block.get("input")})

    usage = data.get("usage", {}) or {}
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    ir, or_ = PRICING.get(model, (3.0, 15.0))
    cost = round((in_tok * ir + out_tok * or_) / 1_000_000, 6)

    return {
        "reply": "\n".join(text_parts).strip(),
        "tool_calls": tool_calls,
        "model": model,
        "stop_reason": data.get("stop_reason"),
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": cost,
        },
        "duration_ms": round((time.time() - start) * 1000, 1),
    }


@router.get("/api/scorr/chat/health")
def scorr_chat_health():
    return {
        "status": "ok",
        "anthropic_api_key_set": bool(ANTHROPIC_API_KEY),
        "admin_token_set": bool(ADMIN_TOKEN),
        "mcp_url": MCP_URL,
        "default_model": DEFAULT_MODEL,
        "sonnet_model": SONNET_MODEL,
        "default_max_tokens": DEFAULT_MAX_TOKENS,
        "sonnet_max_tokens": SONNET_MAX_TOKENS,
    }
