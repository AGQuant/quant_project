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
import re                    # cc#841: cadence classifier
import ast
import json
import logging
import inspect
from datetime import date, datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter

IST = timezone(timedelta(hours=5, minutes=30))   # cc#841: all cadence reasoning is IST

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


# cc#824: jobs that run CHAINED inside another job rather than on their own tick condition. The
# scheduler-loop scanner above derives job names from tick guards, so a chained job is invisible to
# it — and a job the registry cannot see is a job nobody notices has stopped. Listed explicitly.
_CHAINED_JOBS = [
    {"job_name": "screeners_eod", "cadence_human": "nightly, chained immediately after gvm_recompute",
     "module": "screeners_eod.py", "function": "run_all", "service": "app", "category": "chained"},
]


# ── cc#841 part_1: CADENCE-AWARE FRESHNESS ────────────────────────────────────────────────────────
# The flat "stale if last_run > 48h" rule is wrong for most of this registry, and on a weekend it is
# wrong for nearly all of it. Measured on Sunday 02-Aug: 25 jobs flagged stale, of which 23 were
# Friday-session jobs sitting at 2.1-2.4 days (market-hours 5-min loops and weekday-only dailies) and
# one was bg_fu_sync at 6.6 days on a Monday-weekly cadence. Every one of them was healthy. A monitor
# that cries wolf on 24 of 25 rows trains you to ignore it, which is worse than no monitor.
#
# So: a job is STALE only when an EXPECTED RUN WAS MISSED. The cadence expression the registry
# already stores is classified into a class, and each class knows its own allowed silence.
# Deliberately a CLASSIFIER, not a cron parser: these expressions are arbitrary Python read out of
# the dispatch loop, and a half-working parser that silently mis-reads one would be worse than a
# classifier that says "unknown" out loud.

_TRADING_CLOSE_H, _TRADING_CLOSE_M = 15, 30
_MARKET_TICK_GRACE_MIN = 15   # a 5-min loop's last tick lands before the close bell


def _prev_weekday_at(now, hh, mm, weekday=None):
    """Most recent instant at hh:mm that has already passed, restricted to weekdays (or one
    specific weekday). Returns None if none within a 40-day lookback."""
    probe = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if probe > now:
        probe -= timedelta(days=1)
    for _ in range(40):
        ok = (probe.weekday() < 5) if weekday is None else (probe.weekday() == weekday)
        if ok:
            return probe
        probe -= timedelta(days=1)
    return None


def classify_cadence(cadence: str) -> dict:
    """cadence expression -> {'class': ..., 'hh','mm','weekday','dom'} where discoverable."""
    c = (cadence or "").strip()
    cl = c.lower()
    out = {"class": "unknown", "hh": None, "mm": None, "weekday": None, "dom": None,
           "dom_min": None, "months": None}
    if not c:
        return out
    if "armed-flag-only" in cl or "app_config" in cl and "armed" in cl:
        out["class"] = "armed"          # runs only when explicitly armed — silence is normal
        return out
    if "startup" in cl or "on app startup" in cl:
        out["class"] = "startup"
        return out
    if "event-triggered" in cl:
        out["class"] = "event"
        return out
    m = re.search(r"h\s*==\s*(\d+)", c)
    if m:
        out["hh"] = int(m.group(1))
    m = re.search(r"\bm\s*==\s*(\d+)", c)
    if m:
        out["mm"] = int(m.group(1))
    m = re.search(r"weekday\(\)\s*==\s*(\d+)", c)
    if m:
        out["weekday"] = int(m.group(1))
    m = re.search(r"now\.day\s*==\s*(\d+)", c)
    if m:
        out["dom"] = int(m.group(1))
    m = re.search(r"now\.day\s*>=\s*(\d+)", c)
    if m:
        out["dom_min"] = int(m.group(1))
    m = re.search(r"now\.month\s+in\s*\(([\d,\s]+)\)", c)
    if m:
        out["months"] = [int(x) for x in m.group(1).replace(" ", "").split(",") if x]
    if "_is_market_hours" in c:
        out["class"] = "market_hours"
    elif "now.month in" in cl:
        out["class"] = "quarterly"
    elif out["dom"] is not None:
        out["class"] = "monthly"
    elif out["weekday"] is not None:
        out["class"] = "weekly"
    elif out["hh"] is not None:
        out["class"] = "daily"
    elif re.search(r"m\s*%\s*\d+", c) or "every tick" in cl:
        out["class"] = "frequent"
    return out


def expected_last_run(cadence: str, now=None, is_trading_day=None):
    """The most recent instant this job SHOULD have run by. None => cannot be judged on a clock
    (armed / startup / event / unknown), which the caller must report as such rather than as stale."""
    now = now or datetime.now(IST)
    k = classify_cadence(cadence)
    cls = k["class"]
    if cls in ("armed", "startup", "event", "unknown"):
        return None
    if cls == "frequent":
        return now - timedelta(minutes=20)      # generous: a tick loop that missed 20 min is late
    if cls == "market_hours":
        # Healthy all weekend and all evening: the yardstick is the END of the last trading session,
        # minus a grace window. A 5-minute loop's FINAL tick of the day legitimately lands before
        # 15:30 (15:25, or 15:20 if that tick was busy), so demanding a run at-or-after 15:30 would
        # flag every market-hours job as stale every single evening — the very noise this replaces.
        probe = now.replace(hour=_TRADING_CLOSE_H, minute=_TRADING_CLOSE_M, second=0, microsecond=0)
        if probe > now:
            probe -= timedelta(days=1)
        for _ in range(12):
            if probe.weekday() < 5 and (is_trading_day is None or is_trading_day(probe.date())):
                return probe - timedelta(minutes=_MARKET_TICK_GRACE_MIN)
            probe -= timedelta(days=1)
        return None
    hh = k["hh"] if k["hh"] is not None else 23
    mm = k["mm"] if k["mm"] is not None else 59
    if cls == "daily":
        return _prev_weekday_at(now, hh, mm)
    if cls == "weekly":
        return _prev_weekday_at(now, hh, mm, weekday=k["weekday"])
    if cls == "quarterly":
        # The month gate is the whole point of a quarterly job: outside its months there is NOTHING
        # to expect. Reading only the day/hour and ignoring `now.month in (...)` flagged
        # bg_shareholding_quarterly as stale in August, when its next window is October.
        if k["months"] and now.month not in k["months"]:
            return None
        dom = k["dom"] or k["dom_min"] or 1
        probe = now.replace(day=min(dom, 28), hour=hh, minute=mm, second=0, microsecond=0)
        return probe if probe <= now else None
    if cls == "monthly":
        dom = k["dom"] or 1
        probe = now.replace(day=min(dom, 28), hour=hh, minute=mm, second=0, microsecond=0)
        return probe if probe <= now else None
    return None


def job_health(job: dict, now=None, is_trading_day=None) -> dict:
    """{'status','reason','expected_by'} for one scheduler_master row.

    status: ok | stale | never_run | error | unjudgeable
    A MISSED EXPECTED RUN is the only thing that earns 'stale'."""
    now = now or datetime.now(IST)
    # cc#841 part_4/part_6: an INACTIVE row is retired history, not a death. bg_feed_incident_relay
    # (8.2d) and bg_oi_feed_health (9.1d) read as "dead jobs" on the page, but both were retired by
    # cc#660 FEED_GUARDIAN_V1 and already deactivated by cc#759 on 29-Jul — their functions no longer
    # exist in scheduler.py, and feed_guardian's guardian_tick / guardian_offhours own that work now.
    # Their last_run_at is frozen at the retirement. Reviving them would resurrect superseded code;
    # the correct fix is that the registry stops presenting them as live.
    if job.get("active") is False:
        return {"status": "retired", "reason": "inactive — retired, kept for history",
                "expected_by": None, "cadence_class": "retired"}
    cadence = job.get("cadence_human") or ""
    last = job.get("last_run_at")
    exp = expected_last_run(cadence, now, is_trading_day)
    cls = classify_cadence(cadence)["class"]
    if (job.get("last_status") or "") == "error":
        return {"status": "error", "reason": job.get("last_error") or "last run errored",
                "expected_by": (exp.isoformat() if exp else None), "cadence_class": cls}
    if not last:
        if exp is None:
            return {"status": "unjudgeable", "reason": f"{cls} cadence — no clock expectation",
                    "expected_by": None, "cadence_class": cls}
        # cc#841 part_2: a job cannot have missed a run that happened before it EXISTED. Without
        # this, every newly-shipped job reads as never-run against a slot that predates its own
        # deploy — which is precisely what made bg_fetch_universe_reco_news and bg_mf_derived_audit
        # look like real misses when both were simply added after that day's slot had passed.
        added = job.get("added_date")
        if added is not None:
            try:
                a = datetime(added.year, added.month, added.day, tzinfo=IST)
                # added_date is a DATE, so for a slot falling ON the add day we cannot know whether
                # the deploy landed before or after it. That is genuinely unknowable, and saying so
                # beats guessing in either direction — bg_mf_derived_audit (added Sat 01-Aug, slot
                # Sat 07:00) is exactly this case.
                if exp < a + timedelta(days=1):
                    same_day = exp >= a
                    return {"status": "unjudgeable",
                            "reason": (f"added {added} — cannot tell whether the deploy preceded "
                                       f"that day's {cls} slot" if same_day else
                                       f"added {added} — its last {cls} slot predates the job"),
                            "expected_by": exp.isoformat(), "cadence_class": cls}
            except Exception:
                pass
        return {"status": "never_run", "reason": f"expected by {exp.isoformat()}",
                "expected_by": exp.isoformat(), "cadence_class": cls}
    if exp is None:
        return {"status": "ok", "reason": f"{cls} cadence — judged per trigger, not per clock",
                "expected_by": None, "cadence_class": cls}
    if getattr(last, "tzinfo", None) is None:
        last = last.replace(tzinfo=timezone.utc)
    if last < exp:
        return {"status": "stale", "reason": f"missed the run expected by {exp.isoformat()}",
                "expected_by": exp.isoformat(), "cadence_class": cls}
    return {"status": "ok", "reason": "ran at or after its last expected run",
            "expected_by": exp.isoformat(), "cadence_class": cls}


def all_known_jobs():
    return enumerate_scheduler_jobs() + _STARTUP_JOBS + _WORKER_JOBS + _CHAINED_JOBS


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
                # cc#759 fix5: a job registered-but-gone-from-code was previously logged as a low-signal
                # drift note AND left active=TRUE forever — so it re-noted silently every audit and paged
                # nobody (the 6 vanished feed-health jobs are exactly why the 5h outage alerted no one).
                # Formally RETIRE each (active=FALSE, so it self-resolves and never re-alerts) and raise a
                # real 'alert' row. If a retirement is wrong, restore the _spawn in scheduler.py.
                for _name in vanished:
                    cur.execute("""UPDATE scheduler_master
                                   SET active=FALSE,
                                       notes = COALESCE(NULLIF(notes,''), '')
                                               || ' | RETIRED_VANISHED_FROM_CODE cc#759 ' || CURRENT_DATE::text
                                   WHERE job_name=%s""", (_name,))
                _oplog(cur, "scheduler_master_drift",
                       {"direction": "vanished_from_code", "jobs": vanished,
                        "action": "auto-retired active=FALSE (cc#759) — restore the _spawn in scheduler.py if unintended"})
                _oplog(cur, "alert", "scheduler_registry_vanished",
                       {"jobs": vanished,
                        "note": "registered jobs with no code — auto-retired; restore if the retirement is wrong"})
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
        # NOTE: added_date is read BEFORE the row dict is stringified below, because job_health
        # needs it as a date — see the added_date guard in job_health().
        cols = [d[0] for d in cur.description]
        rows = []
        try:
            import scheduler as _s
            _itd = _s._is_trading_day
        except Exception:
            _itd = None
        _now = datetime.now(IST)
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            # cc#841 part_1: the verdict is computed SERVER-SIDE from the cadence the registry
            # already stores, so the page and bg_engine_watchdog can never disagree about what
            # "stale" means — two copies of that judgement is how a monitor starts lying.
            h = job_health(row, _now, _itd)
            row["health"] = h["status"]
            row["health_reason"] = h["reason"]
            row["expected_by"] = h["expected_by"]
            row["cadence_class"] = h["cadence_class"]
            row["added_date"] = str(row["added_date"]) if row["added_date"] else None
            row["last_run_at"] = str(row["last_run_at"]) if row["last_run_at"] else None
            rows.append(row)
    buckets = {}
    for r in rows:
        buckets[r["health"]] = buckets.get(r["health"], 0) + 1
    return {"count": len(rows), "jobs": rows, "health_counts": buckets,
            "health_basis": "cc#841: STALE means an EXPECTED run was missed, computed per cadence "
                            "class — not a flat >48h age."}


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
