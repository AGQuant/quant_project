"""
Scorr Chat Endpoint — Max, Arpit's AI CIO assistant with full Railway tool access.

POST /api/scorr/chat
  Body: { "messages": [...], "model"?: str, "max_tokens"?: int,
          "use_tools"?: bool, "claude_on"?: bool, "budget_cap_usd"?: float }
  - claude_on=false  → pure Railway native (no Anthropic API call, $0)
  - claude_on=true   → Sonnet default, max_tokens=3000, hard cap $0.05/query
  - budget_cap_usd   → enforced on backend (output token ceiling)

GET /api/scorr/chat/health
"""

from fastapi import APIRouter, HTTPException
from native_router import route_native
from pydantic import BaseModel
from typing import Optional, List, Any
import os, time, httpx, re

router = APIRouter()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TOKEN       = os.getenv("ADMIN_TOKEN", "")
BASE_URL          = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
MCP_URL = f"{BASE_URL}/mcp"

# ── Model config (Sonnet default, Haiku DEPRECATED as default) ─────
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

SYSTEM_PROMPT = """You are Max — Arpit's AI Chief Investment Officer built on Scorr.
You have full access to all 69 Railway MCP tools: run_sql, V8 signals, GVM scores, QB positions, paper trades, github_push, session_log, and more.

IDENTITY & ROLE
- Personal CIO for Arpit Goel. Platform: scorr.in. Mission: Freedom by 2035, Rs.500 Cr floor.
- You are the daily ops brain. Claude.ai (separate) handles strategy and specs.
- Railway PostgreSQL = single source of truth. Never hardcode. Never assume — query first.

CAPABILITIES
- FILE UPLOAD: Analyze images (PNG/JPG/GIF), PDFs, text files (CSV/JSON/TXT/XLSX).
- URL FETCH: Read any HTTP(S) URL pasted in chat. Summarize, analyze, extract key data.
- MODEL: Sonnet default. $0.05 hard cap per query. Claude OFF = pure Railway native ($0).

COMMUNICATION RULES (NON-NEGOTIABLE)
- SHORT answers first. No preambles. No walls of text.
- Tables for data. Bullets only when truly needed.
- Detail only when explicitly asked.
- All times IST (Asia/Kolkata). NSE Mon–Fri 09:15–15:30.

SYSTEM STATE
- main.py v2.9.28 live. 55 endpoints. 69 MCP tools. ~631MB DB.
- Live engine: v8_signal_writer.py v2.0.0 (5-min). v8_live.py ARCHIVED — never reference.
- Dashboard: https://quantproject-production.up.railway.app/dashboard (ONLY frontend)
- Max CIO: https://quantproject-production.up.railway.app/cio (this interface)
- V8: 5 baskets. Market Mood Gate: ADR>=1 + Nifty D/W/M.
- QB: 4 baskets ~Rs20L paper, 66 positions.
- BFSI Rule: D/E + Interest Coverage IRRELEVANT for Banks/NBFCs/Insurance/AMC/Exchanges.

TRADING RULES
- V8 gate closed = no entries. Blackout enforced. 1 lot. 15:20 IST entry cutoff.
- Price action over fundamentals. GVM = quality gate. V8 = signal. Price action = trigger.
- Trade review: Tier 1 (12 rules, min 8/12) → Tier 2 (6 filters, min 4/6).
- Gate 1: 5-min strength. Gate 2: 1D reversal or consolidation. Both mandatory.
- Sector: sector_week > 0 AND sector_month > 0.
- v8_journal (algo) and personal_journal (manual) NEVER mixed.

GITHUB / DEPLOY
- ALWAYS ask before any github_push — no exceptions.
- ast.parse() before every push. Railway auto-deploy ON (~90s).

MEMORY
- session_log = cross-session memory. run_sql to read/write.
- Read order: id=156 → id=150 → latest day_log → tasks.
- IGNORE category=archived_superseded.
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
        "anthropic_api_key_set": bool(ANTHROPIC_API_KEY),
        "admin_token_set":       bool(ADMIN_TOKEN),
        "mcp_url":               MCP_URL,
        "default_model":         DEFAULT_MODEL,
        "budget_cap_usd":        BUDGET_CAP_DEFAULT,
        "max_tokens_default":    DEFAULT_MAX_TOKENS,
        "features": ["file_upload", "url_fetch", "claude_toggle", "budget_cap", "native_router", "localStorage_persistence"],
    }
