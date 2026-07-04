"""
V13 Presets Endpoints — Scorr (cc#182, cc#191 scope)
DB-backed saveable filter themes, shared by the V13 (/filters) and V12
(/screener) screeners.

Table v13_presets — one row per named theme. cc#191: a `scope` column ('v13' |
'v12', default 'v13') namespaces themes per screener, so the unique key is
(scope, name) — V12 and V13 can each have a "Momentum" theme independently. A
re-save with the same (scope, name) overwrites (upsert).

  GET    /api/v13/presets?scope=v13   list presets for a scope (default v13)
  POST   /api/v13/presets             create / overwrite by (scope, name)
                                      {name, filters, sort_key, sort_dir, scope}
  PATCH  /api/v13/presets/{pid}       rename {name} (unique within its scope)
  DELETE /api/v13/presets/{pid}       delete

Auth-gated the same way as the screener pages — the scorr_auth session cookie.
The browser sends it automatically on same-origin fetches.
"""
import os
import json
import psycopg
from fastapi import APIRouter, Request, HTTPException

from scorr_auth import _is_authed

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_table():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v13_presets (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                filters JSONB NOT NULL,
                sort_key TEXT,
                sort_dir INT,
                scope TEXT NOT NULL DEFAULT 'v13',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # cc#191: migrate an existing table (had UNIQUE(name)) to per-scope names.
        cur.execute("ALTER TABLE v13_presets ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'v13'")
        cur.execute("ALTER TABLE v13_presets DROP CONSTRAINT IF EXISTS v13_presets_name_key")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS v13_presets_scope_name_uk ON v13_presets(scope, name)")
        conn.commit()


try:
    _ensure_table()
except Exception:
    # DB may be unreachable at import time on cold boot — the table is also
    # created on first write; never block app startup on this.
    pass


def _gate(request: Request):
    if not _is_authed(request):
        raise HTTPException(401, "Not authenticated")


def _row(r):
    return {
        "id": r[0], "name": r[1], "filters": r[2],
        "sort_key": r[3], "sort_dir": r[4], "scope": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
        "updated_at": r[7].isoformat() if r[7] else None,
    }


_COLS = "id,name,filters,sort_key,sort_dir,scope,created_at,updated_at"


@router.get("/api/v13/presets")
def list_presets(request: Request, scope: str = "v13"):
    _gate(request)
    _ensure_table()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM v13_presets WHERE scope=%s ORDER BY LOWER(name)", [scope])
        rows = [_row(r) for r in cur.fetchall()]
    return {"presets": rows, "count": len(rows), "scope": scope}


@router.post("/api/v13/presets")
def save_preset(request: Request, body: dict):
    _gate(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if len(name) > 60:
        raise HTTPException(400, "name too long (max 60)")
    filters = body.get("filters")
    if filters is None:
        raise HTTPException(400, "filters required")
    scope = (body.get("scope") or "v13").strip() or "v13"
    sort_key = body.get("sort_key")
    sort_dir = body.get("sort_dir")
    _ensure_table()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO v13_presets (name, filters, sort_key, sort_dir, scope)
            VALUES (%s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (scope, name) DO UPDATE
              SET filters=EXCLUDED.filters, sort_key=EXCLUDED.sort_key,
                  sort_dir=EXCLUDED.sort_dir, updated_at=NOW()
            RETURNING {_COLS}
        """, [name, json.dumps(filters), sort_key, sort_dir, scope])
        r = cur.fetchone()
        conn.commit()
    return {"status": "ok", "preset": _row(r)}


@router.patch("/api/v13/presets/{pid}")
def rename_preset(request: Request, pid: int, body: dict):
    _gate(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if len(name) > 60:
        raise HTTPException(400, "name too long (max 60)")
    with _conn() as conn, conn.cursor() as cur:
        # uniqueness is per-scope — only clash within the same scope matters
        cur.execute("SELECT 1 FROM v13_presets WHERE name=%s AND id<>%s "
                    "AND scope=(SELECT scope FROM v13_presets WHERE id=%s)", [name, pid, pid])
        if cur.fetchone():
            raise HTTPException(409, "another preset already uses that name")
        cur.execute(f"""
            UPDATE v13_presets SET name=%s, updated_at=NOW()
            WHERE id=%s RETURNING {_COLS}
        """, [name, pid])
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "preset not found")
        conn.commit()
    return {"status": "ok", "preset": _row(r)}


@router.delete("/api/v13/presets/{pid}")
def delete_preset(request: Request, pid: int):
    _gate(request)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM v13_presets WHERE id=%s RETURNING id", [pid])
        r = cur.fetchone()
        conn.commit()
    if not r:
        raise HTTPException(404, "preset not found")
    return {"status": "ok", "deleted": pid}
