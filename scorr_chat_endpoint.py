"""
Scorr Chat Endpoint — free-form chat with full Railway tool access.

POST /api/scorr/chat
  Body: { "messages": [{"role":"user","content":"..."}], "model"?: str, "max_tokens"?: int }
  - Calls Anthropic with the Scorr MCP server attached (server-side).
  - Claude can run run_sql, V8, GVM, session_log, etc. via MCP tools.
  - API key + MCP auth token stay in Railway env. Browser never sees them.

Zero-terminal cockpit: scorr_cockpit.html calls this endpoint.
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

# Cost guardrail
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 2000
HARD_MAX_TOKENS = 4096

# $/million tokens (in, out)
PRICING = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8":   (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

SYSTEM_PROMPT = (
    "You are Claude, acting as Arpit's Scorr quant CIO assistant via the Railway MCP server. "
    "You have full tool access (run_sql, V8, GVM, session_log, etc). "
    "Be short, direct, decisive — answer first, no preambles. Bullets/tables only when needed. "
    "All times IST (Asia/Kolkata). NSE Mon–Fri 9:15–15:30. "
    "Verify technical facts via run_sql / session_log before asserting — never assume. "
    "ALWAYS ask before any github_push. Railway = single source of truth."
)


class ChatMessage(BaseModel):
    role: str
    content: str


class ScorrChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = DEFAULT_MODEL
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS
    use_tools: Optional[bool] = True  # attach MCP server


@router.post("/api/scorr/chat")
async def scorr_chat(req: ScorrChatRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set in Railway env")

    model = req.model or DEFAULT_MODEL
    max_tokens = min(req.max_tokens or DEFAULT_MAX_TOKENS, HARD_MAX_TOKENS)
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

    # Extract text + tool activity from the content blocks
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
        "hard_max_tokens": HARD_MAX_TOKENS,
    }
