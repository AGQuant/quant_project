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
        # cc#558: guarantee the full-universe base column exists before the engine references it
        # (the nightly universe-technicals job also adds it; this covers the deploy->02:05 window).
        try:
            cur.execute("ALTER TABLE universe_technicals ADD COLUMN IF NOT EXISTS vol_ratio_21 NUMERIC")
        except Exception:
            pass
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


# ── cc#461: V13 Theme Bridge engine (session_log id=3069) ─────────────────────────────────────────
# Execute a preset's {fieldkey:{min?,max?}} filters through the REAL metric tables with correct field
# semantics — dma_* in v8_metrics ARE stored as %-distance from the MA (so a direct column bound is
# correct), return_3y/pe/roce from the screener_raw wide dump (text -> numeric). This is the first-class
# engine the Fable bridge validates against instead of approximate raw SQL that mis-handles semantics.
# Whitelist: key -> (table alias, column). m=v8_metrics(latest), g=gvm_scores(latest), s=screener_raw.
# cc#558: base moved v8_metrics (~212 futures) -> universe_technicals u (~1,811 GVM-scored syms).
# Technicals now source "u"; futures-only fields (vol_ratio/sector_*/day_1d) stay "m" via LEFT JOIN
# (NULL for non-futures). Added month_index + vol_ratio_21 (both "u") for the true 52W-breakout preset.
_FIELD_MAP = {
    "dma_20": ("u", "dma_20"), "dma_50": ("u", "dma_50"), "dma_200": ("u", "dma_200"),
    "rsi_month": ("u", "rsi_month"), "rsi_weekly": ("u", "rsi_weekly"), "daily_rsi": ("u", "daily_rsi"),
    "week_return": ("u", "week_return"), "month_return": ("u", "month_return"), "year_return": ("u", "year_return"),
    "week_index_52": ("u", "week_index_52"), "month_index": ("u", "month_index"),
    "mom_2d": ("u", "mom_2d"), "vol_ratio_21": ("u", "vol_ratio_21"),
    "vol_ratio": ("m", "vol_ratio"), "day_1d": ("m", "day_1d"),
    "sector_week": ("m", "sector_week"), "sector_month": ("m", "sector_month"),
    "gvm_score": ("g", "gvm_score"), "g_score": ("g", "g_score"), "v_score": ("g", "v_score"),
    "m_score": ("g", "m_score"), "market_cap": ("g", "market_cap"),
    "return_1y": ("s", "return_1y"), "return_3y": ("s", "return_3y"),
    "return_52w_vs_index": ("s", "return_52w_vs_index"), "pe": ("s", "pe"), "roce": ("s", "roce"),
}


def _col_expr(src, col):
    if src == "s":   # screener_raw is a wide TEXT dump -> strip non-numeric, cast, NULL if not numeric
        return f"NULLIF(REGEXP_REPLACE(s.\"{col}\"::text, '[^0-9.\\-]', '', 'g'), '')::numeric"
    return f'{src}."{col}"'


def _run_screen(cur, filters, sort_key=None, sort_dir=-1, limit=10):
    """Execute {fieldkey:{min?,max?}} against v8_metrics(+gvm_scores+screener_raw). Returns a result dict."""
    filters = filters or {}
    unknown = [k for k in filters if k not in _FIELD_MAP]
    if unknown:
        return {"error": "unknown filter key(s): " + ", ".join(unknown), "valid_keys": sorted(_FIELD_MAP.keys())}
    # cc#558: base = universe_technicals u (latest score_date) — the full ~1,811 GVM-scored universe.
    where = ["u.score_date=(SELECT MAX(score_date) FROM universe_technicals)"]
    params = []
    for k, crit in filters.items():
        expr = _col_expr(*_FIELD_MAP[k])
        if isinstance(crit, dict):
            if crit.get("min") is not None:
                where.append(f"{expr} >= %s"); params.append(crit["min"])
            if crit.get("max") is not None:
                where.append(f"{expr} <= %s"); params.append(crit["max"])
    uses_screener = any(_FIELD_MAP[k][0] == "s" for k in filters)
    uses_fut = any(_FIELD_MAP[k][0] == "m" for k in filters) or (sort_key and _FIELD_MAP.get(sort_key, ("",))[0] == "m")
    # g (gvm) is INNER (every u row is GVM-scored); s + m are LEFT (m = futures-only fields, NULL for
    # the ~1,600 non-futures names — a filter on an m-field therefore narrows back to the futures set).
    join = ("JOIN gvm_scores g ON g.symbol=u.symbol AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores) "
            "LEFT JOIN screener_raw s ON UPPER(s.nse_code)=UPPER(u.symbol) "
            "LEFT JOIN v8_metrics m ON m.symbol=u.symbol AND m.score_date=(SELECT MAX(score_date) FROM v8_metrics)")
    wsql = " AND ".join(where)
    if sort_key and sort_key in _FIELD_MAP:
        order = f"ORDER BY {_col_expr(*_FIELD_MAP[sort_key])} {'ASC' if sort_dir == 1 else 'DESC'} NULLS LAST"
    else:
        order = "ORDER BY g.gvm_score DESC NULLS LAST"
    lim = max(1, min(int(limit or 10), 100))
    cur.execute(f"SELECT COUNT(*) FROM universe_technicals u {join} WHERE {wsql}", params)
    count = int(cur.fetchone()[0])
    cur.execute(f"""SELECT u.symbol, ROUND(g.gvm_score::numeric,2) gvm_score,
                    ROUND(u.month_return::numeric,1) month_return, ROUND(u.week_index_52::numeric,0) week_index_52,
                    ROUND(u.month_index::numeric,0) month_index, ROUND(u.vol_ratio_21::numeric,2) vol_ratio_21,
                    ROUND(u.dma_50::numeric,1) dma_50, ROUND(u.dma_200::numeric,1) dma_200
                    FROM universe_technicals u {join} WHERE {wsql} {order} LIMIT {lim}""", params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    scope = "full universe ~1,811 (universe_technicals)" + (" ∩ screener_raw fundamentals" if uses_screener else "") \
            + (" ∩ ~212 futures (m-field filter)" if uses_fut else "")
    return {"count": count, "scope_used": scope, "rows": rows,
            "note": "dma_* = %-distance from the MA; base is the full ~1,811 GVM universe (universe_technicals). "
                    "Futures-only fields (vol_ratio/sector_week/sector_month/day_1d) are LEFT-joined from v8_metrics "
                    "and are NULL for the ~1,600 non-futures names — filtering on them narrows back to the futures set."}


@router.post("/api/v13/theme/run")
def theme_run(body: dict):
    """cc#461 tool_1: execute filters through the real engine WITHOUT saving (Fable's pre-save validation)."""
    with _conn() as conn, conn.cursor() as cur:
        return _run_screen(cur, body.get("filters") or {}, body.get("sort_key"),
                           body.get("sort_dir", -1), body.get("limit", 10))


@router.post("/api/v13/theme/save")
def theme_save_validated(body: dict):
    """cc#461 tool_2: validate keys, run the screen, REFUSE count=0 or >500, upsert v13_presets -> {id,count}."""
    name = (body.get("name") or "").strip()
    filters = body.get("filters") or {}
    if not name:
        return {"error": "name required"}
    unknown = [k for k in filters if k not in _FIELD_MAP]
    if unknown:
        return {"error": "unknown filter key(s): " + ", ".join(unknown), "valid_keys": sorted(_FIELD_MAP.keys())}
    sort_key = body.get("sort_key")
    sort_dir = 1 if body.get("sort_dir") == 1 else -1
    with _conn() as conn, conn.cursor() as cur:
        res = _run_screen(cur, filters, sort_key, sort_dir, 5)
        cnt = res.get("count", 0)
        if cnt == 0:
            return {"refused": "count=0 — the filters match no stocks; loosen them", "count": 0}
        if cnt > 500:
            return {"refused": f"count={cnt} (>500) — too broad; tighten the filters", "count": cnt}
        _ensure_table()
        mode = (body.get("mode") or "insert").lower()
        pid = body.get("id")
        if mode == "update" and pid:
            cur.execute("""UPDATE v13_presets SET name=%s, filters=%s, sort_key=%s, sort_dir=%s,
                           scope='global', updated_at=NOW() WHERE id=%s RETURNING id""",
                        [name, json.dumps(filters), sort_key, sort_dir, pid])
            row = cur.fetchone()
            if not row:
                return {"error": f"id {pid} not found for update"}
            new_id = row[0]
        else:
            cur.execute("""INSERT INTO v13_presets (name, filters, sort_key, sort_dir, scope)
                           VALUES (%s,%s,%s,%s,'global')
                           ON CONFLICT (scope, name) DO UPDATE SET filters=EXCLUDED.filters,
                             sort_key=EXCLUDED.sort_key, sort_dir=EXCLUDED.sort_dir, updated_at=NOW()
                           RETURNING id""", [name, json.dumps(filters), sort_key, sort_dir])
            new_id = cur.fetchone()[0]
        conn.commit()
    return {"id": new_id, "count": cnt, "name": name, "scope_used": res.get("scope_used")}


@router.get("/api/v13/theme/list")
def theme_list():
    """cc#461 tool_3: all global theme presets with id, name, filter summary."""
    with _conn() as conn, conn.cursor() as cur:
        _ensure_table()
        cur.execute("SELECT id, name, filters, sort_key, sort_dir FROM v13_presets WHERE scope='global' ORDER BY LOWER(name)")
        out = []
        for r in cur.fetchall():
            f = r[2] or {}
            summ = ", ".join(
                f"{k}{('>=' + str(v['min'])) if isinstance(v, dict) and v.get('min') is not None else ''}"
                f"{('<=' + str(v['max'])) if isinstance(v, dict) and v.get('max') is not None else ''}"
                for k, v in f.items()) if isinstance(f, dict) else str(f)
            out.append({"id": r[0], "name": r[1], "filters": f, "summary": summ,
                        "sort_key": r[3], "sort_dir": r[4]})
    return {"presets": out, "count": len(out)}
