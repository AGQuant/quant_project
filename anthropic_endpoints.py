"""
Anthropic API Integration — MCP Wrapper for Scorr
Routes Claude API calls through Railway, logs usage to session_log.
File: anthropic_endpoints.py (v1.0.0)
"""

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import os
import json
import logging
from datetime import datetime
import psycopg
from anthropic import Anthropic

log = logging.getLogger("scorr.anthropic")

router = APIRouter(prefix="/api/anthropic", tags=["anthropic"])

DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_conn():
    """Get PostgreSQL connection."""
    try:
        return psycopg.connect(DATABASE_URL, autocommit=True)
    except Exception as e:
        log.error(f"DB connection failed: {e}")
        return None


class MessageRequest(BaseModel):
    """Claude message request."""
    prompt: str
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    system: Optional[str] = None
    temperature: Optional[float] = 0.7


class MessageResponse(BaseModel):
    """Claude message response."""
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    request_id: str


@router.post("/chat", response_model=MessageResponse)
async def anthropic_chat(request: MessageRequest, x_admin_token: Optional[str] = Header(None)):
    """
    POST /api/anthropic/chat
    Call Claude via Anthropic API. Logs usage to session_log.
    
    Query params: prompt, model, max_tokens, system
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    
    try:
        # Build messages
        messages = [{"role": "user", "content": request.prompt}]
        
        # Call Anthropic
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system or "You are Scorr, an AI Chief Investment Officer.",
            messages=messages,
            temperature=request.temperature or 0.7
        )
        
        # Extract response
        reply = response.content[0].text if response.content else ""
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        request_id = response.id
        
        # Log to session_log
        conn = get_db_conn()
        if conn:
            try:
                details = {
                    "prompt": request.prompt[:200],  # First 200 chars
                    "model": request.model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "stop_reason": response.stop_reason,
                    "request_id": request_id
                }
                conn.execute(
                    """
                    INSERT INTO session_log (session_date, session_ts, category, title, details)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        datetime.now().date(),
                        datetime.now(),
                        "anthropic_api_call",
                        f"Chat via API — {request.model}",
                        json.dumps(details)
                    )
                )
                conn.close()
            except Exception as e:
                log.error(f"Failed to log to session_log: {e}")
        
        return MessageResponse(
            reply=reply,
            model=request.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=response.stop_reason,
            request_id=request_id
        )
    
    except Exception as e:
        log.error(f"Anthropic API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/usage")
async def get_usage(x_admin_token: Optional[str] = Header(None)):
    """
    GET /api/anthropic/usage
    Fetch Anthropic API usage from session_log.
    """
    try:
        conn = get_db_conn()
        if not conn:
            raise HTTPException(status_code=500, detail="DB connection failed")
        
        # Fetch last 50 API calls
        cursor = conn.execute(
            """
            SELECT session_ts, title, details 
            FROM session_log 
            WHERE category = 'anthropic_api_call'
            ORDER BY session_ts DESC 
            LIMIT 50
            """
        )
        
        rows = cursor.fetchall()
        conn.close()
        
        # Parse details JSON
        usage_data = []
        total_input = 0
        total_output = 0
        
        for ts, title, details_json in rows:
            try:
                details = json.loads(details_json)
                usage_data.append({
                    "timestamp": ts.isoformat(),
                    "model": details.get("model"),
                    "input_tokens": details.get("input_tokens", 0),
                    "output_tokens": details.get("output_tokens", 0),
                    "request_id": details.get("request_id")
                })
                total_input += details.get("input_tokens", 0)
                total_output += details.get("output_tokens", 0)
            except json.JSONDecodeError:
                pass
        
        return {
            "usage_log": usage_data,
            "summary": {
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_requests": len(usage_data),
                "estimated_cost_usd": round((total_input * 0.003 + total_output * 0.015) / 1_000_000, 4)
            }
        }
    
    except Exception as e:
        log.error(f"Usage fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """GET /api/anthropic/health — Check if API is configured."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    return {
        "status": "ok" if api_key else "not_configured",
        "api_key_present": bool(api_key)
    }
