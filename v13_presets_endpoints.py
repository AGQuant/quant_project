"""
V13 Presets Endpoints — Scorr (cc#182)
DB-backed saveable filter themes for the /filters unified screener.

Table v13_presets — one row per named theme, `name` is the unique key so a
re-save with the same name overwrites (upsert). Kept generic (no V13-specific
columns) so the same table/endpoints can back V12 later.

  GET    /api/v13/presets            list all presets
  POST   /api/v13/presets            create / overwrite by name
                                     {name, filters, sort_key, sort_dir}
  PATCH  /api/v13/presets/{pid}      rename {name}
  DELETE /api/v13/presets/{pid}      delete

Auth-gated the same way as the /filters page — the scorr_auth session cookie.
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
                name TEXT NOT NULL UNIQUE,
                filters JSONB NOT NULL,
                sort_key TEXT,
                sort_dir INT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
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
        "sort_key": r[3], "sort_dir": r[4],
        "created_at": r[5].isoformat() if r[5] else None,
        "updated_at": r[6].isoformat() if r[6] else None,
    }


_COLS = "id,name,filters,sort_key,sort_dir,created_at,updated_at"


@router.get("/api/v13/presets")
def list_presets(request: Request):
    _gate(request)
    _ensure_table()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM v13_presets ORDER BY LOWER(name)")
        rows = [_row(r) for r in cur.fetchall()]
    return {"presets": rows, "count": len(rows)}


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
    sort_key = body.get("sort_key")
    sort_dir = body.get("sort_dir")
    _ensure_table()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO v13_presets (name, filters, sort_key, sort_dir)
            VALUES (%s, %s::jsonb, %s, %s)
            ON CONFLICT (name) DO UPDATE
              SET filters=EXCLUDED.filters, sort_key=EXCLUDED.sort_key,
                  sort_dir=EXCLUDED.sort_dir, updated_at=NOW()
            RETURNING {_COLS}
        """, [name, json.dumps(filters), sort_key, sort_dir])
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
        cur.execute("SELECT 1 FROM v13_presets WHERE name=%s AND id<>%s", [name, pid])
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
