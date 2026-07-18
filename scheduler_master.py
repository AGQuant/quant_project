"""
scheduler_master.py -- cc#525 MASTER SCHEDULER REGISTRY.

Single source of truth for every scheduled/recurring job, built by parsing scheduler.py's
own source (not hand-maintained docs) so the registry can never silently drift from the code
that actually runs. See cc_tasks id=525 for the founder spec.

HONESTY NOTE: the spec assumes this app uses APScheduler ("all APScheduler add_job/cron/
interval registrations"). It does not -- grepped scheduler.py, main.py, and worker/
fyers_feed.py for apscheduler/AsyncIOScheduler/add_job/BackgroundScheduler: zero matches.
The real mechanism is a hand-rolled 60-second tick loop (scheduler.py's _scheduler_loop,
driven by _supervisor) that dispatches jobs via _spawn(fn) inside an if/elif chain gated on
(h, m, weekday, day-of-month) conditions. This module's enumeration therefore AST-parses
_scheduler_loop's source directly (walk_scheduler_spawns below) rather than querying an
APScheduler registry that doesn't exist -- the same "code is the source of truth" intent,
adapted to the codebase that's actually here.
"""
import os
import ast
import json
import logging
import inspect
from datetime import date

import psycopg
from fastapi import APIRouter

log = logging.getLogger("scorr.scheduler_master")
router = APIRouter(tags=["scheduler_master"])

_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_DB)


def _oplog(cur, title, details, category="scheduler_master"):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)",
                    (category, title, json.dumps(details, default=str)))
    except Exception as e:
        log.warning(f"oplog {title}: {e}")


def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS scheduler_master (
        job_name TEXT PRIMARY KEY, module TEXT, function TEXT, cadence_human TEXT,
        cron_expr TEXT, time_window TEXT, service TEXT DEFAULT 'app',
        category TEXT DEFAULT 'uncategorized', active BOOLEAN DEFAULT TRUE,
        added_date DATE DEFAULT CURRENT_DATE, notes TEXT,
        last_run_at TIMESTAMPTZ, last_status TEXT, last_error TEXT, last_duration_ms INTEGER)""")


# ── 1. CODE ENUMERATION (AST-derived, not hand-maintained) ─────────────────────

def _cond_src(test_node):
    try:
        return ast.unparse(test_node)
    except Exception:
        return "?"


def _walk_spawns(node, conditions, out):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If):
            _walk_spawns(child, conditions + [_cond_src(child.test)], out)
        elif isinstance(child, ast.Expr) and isinstance(child.value, ast.Call):
            call = child.value
            try:
                fname = ast.unparse(call.func)
            except Exception:
                fname = ""
            if fname == "_spawn" and call.args:
                try:
                    job = ast.unparse(call.args[0])
                except Exception:
                    continue
                out.append({"job": job.lstrip("_"), "conditions": conditions[:], "lineno": child.lineno})
        else:
            _walk_spawns(child, conditions, out)


# cc#526 item 4: a handful of jobs are dispatched UNCONDITIONALLY every tick (no if-gate at the
# _spawn call site) but self-gate INSIDE their own function body against an app_config flag or
# an internal day/hour check -- the simple AST walker above only sees the dispatch call, not
# the body, so it reported "every tick (unconditional)" for these, which is technically true of
# the dispatch but misleading about the job's real cadence. Manual override for the jobs whose
# real gate lives inside the function (checked against scheduler.py by hand; update this map if
# one of these functions' internal gating ever changes).
_CADENCE_OVERRIDES = {
    "bg_ops_metrics_backfill": "armed-flag-only (app_config ops_metrics_backfill_run='pending'); "
                                "checked every tick, runs only when armed",
    "bg_ops_metrics_t1": "daily ~08:00-08:10 IST (self-gated inside the function; day-locked)",
    "bg_ops_metrics_saturday": "Saturdays 10:00-10:10 IST (self-gated inside the function; day-locked)",
    "bg_ops_metrics_season_sweep": "1st of Sep/Dec/Mar/Jun, 10:00-10:10 IST (self-gated inside the function; month-locked)",
    "bg_mf_mc_discover": "armed-flag-only (app_config mf_mc_discover_run); checked every tick, runs only when armed",
    "bg_mf_mc_oneshot": "armed-flag-only (app_config mf_mc_oneshot_run); checked every tick, runs only when armed",
    "bg_ops_text_fetch": "armed-flag-only (app_config ops_text_fetch_run='pending'), window-gated "
                          "23:00-06:00 IST; checked every tick, runs only when armed and in-window",
}


def enumerate_scheduler_jobs():
    """AST-parses scheduler.py's _scheduler_loop for every _spawn(fn) call site, paired with
    its enclosing if-condition chain (the job's real cadence, straight from the dispatch
    logic). This is the CODE-authoritative list the drift audit diffs against."""
    import scheduler as _sched_mod
    src = inspect.getsource(_sched_mod)
    tree = ast.parse(src)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "_scheduler_loop"), None)
    if target is None:
        return []
    raw = []
    _walk_spawns(target, [], raw)
    # de-dupe by (job, conditions) -- a job spawned at multiple distinct times (e.g. NSE EOD
    # ingest at 18:30/19:30/20:30) gets one row per distinct condition, matching how the
    # founder's own Master Brief checklist lists retries as part of the same job's cadence.
    by_job = {}
    for r in raw:
        by_job.setdefault(r["job"], []).append(" AND ".join(r["conditions"]) or "every tick (unconditional)")
    jobs = []
    for job, conds in by_job.items():
        cadence = _CADENCE_OVERRIDES.get(job, "; ".join(sorted(set(conds))))
        jobs.append({"job_name": job, "cadence_human": cadence,
                     "module": "scheduler.py", "function": job, "service": "app",
                     "category": "scheduler_loop"})
    return jobs


# Startup-triggered one-shots (main.py @app.on_event("startup")) -- not on the tick loop's
# cadence but still recurring in the sense that they fire on every deploy (Railway auto-deploys
# ~90s per push per CLAUDE.md), so they belong in the registry too.
_STARTUP_JOBS = [
    {"job_name": "init_tables", "cadence_human": "on app startup", "module": "main.py",
     "function": "_init_tables", "service": "app", "category": "startup"},
    {"job_name": "auto_fill_briefs", "cadence_human": "on app startup (+15s), idempotent (skips if fully cached)",
     "module": "main.py", "function": "_auto_fill_briefs", "service": "app", "category": "startup"},
    {"job_name": "v8_paper_rebuild_cutover", "cadence_human": "on app startup (+20s), idempotent one-time cutover",
     "module": "main.py", "function": "_v8_paper_rebuild_cutover", "service": "app", "category": "startup"},
    {"job_name": "scheduler_master_startup_audit", "cadence_human": "on app startup (seeds + drift-audits this very registry)",
     "module": "scheduler.py", "function": "_bg_scheduler_master_startup_audit", "service": "app", "category": "startup"},
]

# Worker-internal loops (worker/fyers_feed.py) -- in-process, not dispatched by scheduler.py's
# tick loop, so the AST enumeration above can't see them; listed by inspection (see cc_tasks
# id=525 result for the honesty note on why these are hand-entered, not code-derived like the
# scheduler.py rows above).
_WORKER_JOBS = [
    {"job_name": "fyers_feed_watchdog", "cadence_human": "every ~5 min (in-process while-loop, HEALTH_LOG_MINS)",
     "module": "worker/fyers_feed.py", "function": "main loop feed watchdog", "service": "worker",
     "category": "worker_watchdog", "notes": "reconnects on per-source symbol-count floor breach (cc#489)"},
    {"job_name": "fyers_token_relogin", "cadence_human": "event-triggered on auth/token failure",
     "module": "worker/fyers_feed.py", "function": "try_relogin call sites", "service": "worker",
     "category": "worker_watchdog"},
]


def all_known_jobs():
    return enumerate_scheduler_jobs() + _STARTUP_JOBS + _WORKER_JOBS


# ── 2 & 3. SEED + RUN RECORDER ──────────────────────────────────────────────────

def seed_registry(conn=None):
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            jobs = all_known_jobs()
            for j in jobs:
                cur.execute("""INSERT INTO scheduler_master
                    (job_name, module, function, cadence_human, service, category, active, added_date, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,TRUE,CURRENT_DATE,%s)
                    ON CONFLICT (job_name) DO UPDATE SET
                      module=EXCLUDED.module, function=EXCLUDED.function,
                      cadence_human=EXCLUDED.cadence_human, service=EXCLUDED.service,
                      category=EXCLUDED.category""",
                            (j["job_name"], j["module"], j["function"], j["cadence_human"],
                             j["service"], j["category"], j.get("notes")))
            conn.commit()
        return {"seeded": len(jobs)}
    finally:
        if own:
            conn.close()


def record_run(job_name, status, error=None, duration_ms=None):
    """Zero behavior change to the job itself: called from scheduler.py's _spawn wrapper AFTER
    the job runs, success or failure. Never raises -- a recorder failure must never surface as
    a job failure. Doubles as live auto-registration: a job that fires but isn't yet in
    scheduler_master gets inserted on the spot (notes=AUTO_REGISTERED_DRIFT), same as the daily
    audit would eventually do, just immediate instead of up-to-a-day late."""
    job_name = (job_name or "").lstrip("_")
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("""UPDATE scheduler_master SET last_run_at=NOW(), last_status=%s,
                           last_error=%s, last_duration_ms=%s WHERE job_name=%s""",
                        (status, (error or "")[:400] if error else None, duration_ms, job_name))
            if cur.rowcount == 0:
                cur.execute("""INSERT INTO scheduler_master
                    (job_name, module, function, cadence_human, service, category, active,
                     added_date, notes, last_run_at, last_status, last_error, last_duration_ms)
                    VALUES (%s,'scheduler.py',%s,'unknown (auto-registered by run recorder)',
                            'app','uncategorized',TRUE,CURRENT_DATE,'AUTO_REGISTERED_DRIFT',
                            NOW(),%s,%s,%s)
                    ON CONFLICT (job_name) DO NOTHING""",
                            (job_name, job_name, status, (error or "")[:400] if error else None, duration_ms))
                if cur.rowcount:
                    _oplog(cur, "scheduler_master_drift",
                           {"job_name": job_name, "reason": "ran but was missing from scheduler_master"})
            conn.commit()
    except Exception as e:
        log.warning(f"scheduler_master.record_run failed for {job_name}: {e}")


# ── 4. DRIFT AUDIT ───────────────────────────────────────────────────────────────

def run_drift_audit(conn=None):
    """Daily 08:45 IST + on app startup: diff the AST-derived code enumeration against
    scheduler_master. Code job missing from master -> auto-INSERT (notes=AUTO_REGISTERED_DRIFT)
    + ops_log alert. Active master row with no matching code job -> ops_log alert (vanished)."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            code_jobs = {j["job_name"]: j for j in all_known_jobs()}
            cur.execute("SELECT job_name, active FROM scheduler_master")
            master_jobs = {r[0]: r[1] for r in cur.fetchall()}

            missing_from_master = [j for j in code_jobs if j not in master_jobs]
            for name in missing_from_master:
                j = code_jobs[name]
                cur.execute("""INSERT INTO scheduler_master
                    (job_name, module, function, cadence_human, service, category, active, added_date, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,TRUE,CURRENT_DATE,'AUTO_REGISTERED_DRIFT')
                    ON CONFLICT (job_name) DO NOTHING""",
                            (j["job_name"], j["module"], j["function"], j["cadence_human"],
                             j["service"], j["category"]))

            vanished = [n for n, active in master_jobs.items() if active and n not in code_jobs]

            if missing_from_master:
                _oplog(cur, "scheduler_master_drift",
                       {"direction": "missing_from_master", "jobs": missing_from_master})
            if vanished:
                _oplog(cur, "scheduler_master_drift",
                       {"direction": "vanished_from_code", "jobs": vanished})
            summary = {"code_jobs": len(code_jobs), "master_jobs": len(master_jobs),
                       "missing_from_master": missing_from_master, "vanished_from_code": vanished}
            _oplog(cur, "scheduler_master_audit_run", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


# ── 5. SURFACE ────────────────────────────────────────────────────────────────

@router.get("/api/scheduler/master")
def get_scheduler_master():
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT job_name, module, function, cadence_human, service, category,
                              active, added_date, notes, last_run_at, last_status, last_error,
                              last_duration_ms
                       FROM scheduler_master ORDER BY service, category, job_name""")
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            row["added_date"] = str(row["added_date"]) if row["added_date"] else None
            row["last_run_at"] = str(row["last_run_at"]) if row["last_run_at"] else None
            rows.append(row)
    return {"count": len(rows), "jobs": rows}


@router.post("/api/admin/scheduler_master/seed")
def admin_seed(token: str = ""):
    from fundamentals_scraper import _check_admin
    _check_admin(token)
    return seed_registry()


@router.post("/api/admin/scheduler_master/audit")
def admin_audit(token: str = ""):
    from fundamentals_scraper import _check_admin
    _check_admin(token)
    return run_drift_audit()
