"""
Scheduler Health Monitor — Scorr (cc_task #16, 19-Jun-2026)

GET /api/health/scheduler — exposes live-tick freshness so a stalled scheduler
is visible instead of failing silently.

Liveness signal = MAX(computed_at) from v8_metrics (the live signal writer stamps
every row with NOW() each 5-min tick). Minutes-since is computed with the DB clock
(NOW() - MAX) so it is timezone-consistent regardless of server/DB tz.

Status:
  OK    — last tick <= 7 min (during market hours)
  STALE — 7 < last tick <= 15 min
  DEAD  — last tick > 15 min
  IDLE  — outside market hours (no ticks expected)
  NO_DATA — no v8_metrics rows at all
"""

import os
import psycopg
from fastapi import APIRouter

import scheduler

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

STALE_MIN = 7
DEAD_MIN = 15


@router.get("/api/health/scheduler")
def health_scheduler():
    last_ts = None
    mins = None
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(computed_at),
                       EXTRACT(EPOCH FROM (NOW() - MAX(computed_at)))/60.0
                FROM v8_metrics
            """)
            row = cur.fetchone()
        if row:
            last_ts = row[0].isoformat() if row[0] is not None else None
            mins = round(float(row[1]), 2) if row[1] is not None else None
    except Exception as e:
        return {"status": "ERROR", "error": str(e),
                "last_tick_ts": None, "minutes_since_last_tick": None}

    market_hours = scheduler._is_market_hours(scheduler._ist_now())

    if mins is None:
        status = "NO_DATA"
    elif not market_hours:
        status = "IDLE"
    elif mins > DEAD_MIN:
        status = "DEAD"
    elif mins > STALE_MIN:
        status = "STALE"
    else:
        status = "OK"

    return {
        "status": status,
        "last_tick_ts": last_ts,
        "minutes_since_last_tick": mins,
        "market_hours": market_hours,
        "thresholds": {"stale_min": STALE_MIN, "dead_min": DEAD_MIN},
        **scheduler.health_state(),
    }

# ---------------------------------------------------------------------------
# cc#1049 measurement probe (Fable, 16-Aug-2026) — READ-ONLY
# GET /api/health/memory — live RSS + module-level cache attribution so the
# memory-trim card is fixed from evidence, not guesses. No state is mutated.
# ---------------------------------------------------------------------------
import sys as _sys
import gc as _gc
import threading as _threading


def _obj_size_mb(obj):
    """Best-effort size estimate in MB. DataFrames deep-measured; containers
    sampled (first 50 items) and extrapolated; everything else shallow."""
    try:
        _pd = _sys.modules.get("pandas")
        if _pd is not None:
            if isinstance(obj, _pd.DataFrame):
                return float(obj.memory_usage(deep=True).sum()) / 1048576.0
            if isinstance(obj, _pd.Series):
                return float(obj.memory_usage(deep=True)) / 1048576.0
    except Exception:
        pass
    try:
        base = _sys.getsizeof(obj)
        if isinstance(obj, (dict, list, set, tuple)) and len(obj) > 0:
            n = len(obj)
            sample_n = min(n, 50)
            sample = 0
            if isinstance(obj, dict):
                for k, v in list(obj.items())[:sample_n]:
                    sample += _sys.getsizeof(k) + _sys.getsizeof(v)
            else:
                for it in list(obj)[:sample_n]:
                    sample += _sys.getsizeof(it)
            return (base + (sample / sample_n) * n) / 1048576.0
        return base / 1048576.0
    except Exception:
        return 0.0


def _proc_rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


@router.get("/api/health/memory")
def health_memory(top: int = 30, min_mb: float = 1.0):
    """Read-only heap census: RSS, biggest module-level globals, type counts."""
    rss = _proc_rss_mb()

    # 1) Module-level globals above min_mb — the idle-residency suspects
    heavy = []
    seen = set()
    for mod_name, mod in list(_sys.modules.items()):
        if mod is None:
            continue
        try:
            g = vars(mod)
        except Exception:
            continue
        for var_name, obj in list(g.items()):
            if var_name.startswith("__"):
                continue
            oid = id(obj)
            if oid in seen:
                continue
            if isinstance(obj, (type(_sys), type)) or callable(obj):
                continue
            mb = _obj_size_mb(obj)
            if mb >= min_mb:
                seen.add(oid)
                try:
                    ln = len(obj)
                except Exception:
                    ln = None
                heavy.append({
                    "module": mod_name,
                    "name": var_name,
                    "type": type(obj).__name__,
                    "size_mb": round(mb, 2),
                    "len": ln,
                })
    heavy.sort(key=lambda r: -r["size_mb"])

    # 2) gc census — top types by instance count
    counts = {}
    try:
        for o in _gc.get_objects():
            t = type(o).__name__
            counts[t] = counts.get(t, 0) + 1
    except Exception:
        pass
    top_types = sorted(counts.items(), key=lambda kv: -kv[1])[:15]

    return {
        "rss_mb": rss,
        "python_modules_loaded": len(_sys.modules),
        "threads": _threading.active_count(),
        "gc_objects_total": sum(counts.values()) if counts else None,
        "gc_top_types": [{"type": t, "count": c} for t, c in top_types],
        "module_globals_over_min_mb": heavy[:top],
        "note": "sizes are estimates; DataFrames deep-measured, containers sampled",
    }


def _persist_probe(label):
    import json as _json
    try:
        payload = health_memory(top=30, min_mb=1.0)
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO session_log (session_date, session_ts, category, title, details) "
                "VALUES (CURRENT_DATE, NOW(), 'memory_probe', %s, %s::jsonb)",
                (
                    "MEMORY_PROBE %s rss_mb=%s" % (label, payload.get("rss_mb")),
                    _json.dumps(payload, default=str),
                ),
            )
            conn.commit()
    except Exception:
        pass  # probe must never affect the app


def _boot_probe_once():
    """Census 120s after boot (idle baseline), then every 4h (growth curve),
    persisted to session_log(category='memory_probe') so the RSS ratchet is
    measurable via run_sql. cc#1049 evidence: boot RSS was 256MB vs a 3-4GB
    Railway baseline — the cost is runtime accumulation, and this loop shows
    which windows add it."""
    import time as _time
    _time.sleep(120)
    _persist_probe("boot baseline")
    while True:
        _time.sleep(4 * 3600)
        _persist_probe("4h growth")


try:
    _t = _threading.Thread(target=_boot_probe_once, daemon=True, name="memprobe")
    _t.start()
except Exception:
    pass


# ---------------------------------------------------------------------------
# cc#1065 GVM FIGHT CARD route (Fable, 16-Aug-2026)
# GET /m/gvm2 — serves the live mobile GVM fight card (scorr_gvm_fightcard.html),
# which fetches /api/gvm/company/{symbol} client-side. Hosted here temporarily
# because this router is small and proven-mounted; relocate to a gvm router in
# the next cleanup card. Read-only file serve; no state.
# ---------------------------------------------------------------------------
from fastapi.responses import HTMLResponse, PlainTextResponse

_FIGHTCARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "scorr_gvm_fightcard.html")


@router.get("/m/gvm2")
def gvm_fightcard():
    try:
        with open(_FIGHTCARD_FILE, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception as e:
        return PlainTextResponse("fight card unavailable: %s" % e, status_code=500)
