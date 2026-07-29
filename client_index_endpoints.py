"""
client_index_endpoints.py — cc#758: admin-only credential-management routes for client_index.

All routes are X-Admin-Token gated (same ADMIN_TOKEN scheme as the other admin routers). None of them
ever returns a stored password/totp_key EXCEPT /reveal, which decrypts ONE field for ONE account and
writes a session_log audit row first. Read/list routes for client_index (cc#756/757) must run every row
through client_index_security.scrub_row().
"""

import os
import psycopg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import client_index_security as cis

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _require_admin(token):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="admin token required")


@router.get("/api/admin/client-index/security-status")
def security_status(x_admin_token: str = Header(None)):
    _require_admin(x_admin_token)
    with psycopg.connect(_DB) as conn:
        return cis.security_status(conn)


@router.post("/api/admin/client-index/encrypt-migrate")
def encrypt_migrate(x_admin_token: str = Header(None)):
    """Idempotently encrypt every still-plaintext password/totp_key in place. Also runs automatically on
    app startup when CLIENT_INDEX_ENC_KEY is set — this route is the manual trigger + verification."""
    _require_admin(x_admin_token)
    if not cis.key_present():
        raise HTTPException(status_code=400,
                            detail="CLIENT_INDEX_ENC_KEY env var is not set on Railway — set it first")
    with psycopg.connect(_DB) as conn:
        return cis.migrate_encrypt(conn)


class RevealRequest(BaseModel):
    account_id: str
    field: str   # 'password' | 'totp_key'


@router.post("/api/admin/client-index/reveal")
def reveal(req: RevealRequest, x_admin_token: str = Header(None)):
    """Admin single-field decrypt. Audit-logged to session_log BEFORE the value is returned."""
    _require_admin(x_admin_token)
    if req.field not in cis.SECRET_COLUMNS:
        raise HTTPException(status_code=400, detail=f"field must be one of {list(cis.SECRET_COLUMNS)}")
    try:
        with psycopg.connect(_DB) as conn:
            value = cis.decrypt_field(conn, req.account_id, req.field, actor="admin_reveal")
    except KeyError:
        raise HTTPException(status_code=404, detail="account_id not found")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"account_id": req.account_id, "field": req.field, "value": value, "audited": True}
