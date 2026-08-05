"""
model_launcher.py — cc#860 MODEL LAUNCHER (founder 05-Aug-2026).

One button, every model, and a badge that tells the truth about whether each one is running.

GOVERNING RULE — ENGINE_LIVENESS_RULE_V1 (session_log 13829): a LIVE badge MUST derive from actual
run data. Registration alone is never enough. So every state here comes from
scheduler_master.last_run_at / last_status, never from the fact that a row exists.

ENUMERATION IS REGISTRY-DERIVED, TWICE OVER (SCHEDULER_MASTER_RULE 5700). There is NO model list in
this file. Models come from:

    scheduler_master (active = true)  JOIN  model_registry USING (job_name)  WHERE is_public

model_registry is owned and seeded by Claude.ai per ROLE_CHARTER_V1 (15663 — all DB writes). If a
model is missing from the launcher the fix is a registry row, NOT an edit here. A hardcoded model
list is the exact defect this card exists to prevent, and grepping this file for one is cc#860's V1.

A registry row whose job_name has no active scheduler_master row is NOT silently dropped — it is
returned in `orphans` so the liveness gap is visible. That is the card's stop_condition surfaced as
data instead of a silent omission.

THE HARD PART: "STALE" IS RELATIVE TO EACH JOB'S OWN CADENCE.
A single global staleness threshold is wrong and the live data proves it — bg_signal_writer at 4
minutes old is healthy on a 5-minute tick, while bg_shareholding_quarterly at 7,879 minutes old is
equally healthy on "Jan/Apr/Jul/Oct, day >= 25". One threshold would call the second one dead.
So the window is derived per job from its OWN cadence_human string.

AND THE TRAP UNDERNEATH IT: MARKET-GATED JOBS DO NOT RUN OUT OF HOURS.
Most models tick only inside the NSE session. At 21:00 IST a 5-minute market-hours job is ~5.5 hours
stale and perfectly healthy. Calling that STALE would light the whole launcher red every evening —
which is the cc#841 false-positive class, where market-hours jobs were flagged every night until a
grace window was added. Market-gated jobs are therefore judged against the SESSION, not the clock.
"""

import os
import re
import logging
from datetime import datetime, time as dt_time
from typing import Dict, Any, Optional, Tuple

import psycopg2
import pytz
from fastapi import APIRouter

log = logging.getLogger("scorr.model_launcher")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = pytz.timezone("Asia/Kolkata")

# Grace multiplier on the derived interval — a tick that lands a little late is not a dead engine.
GRACE_MULT = 2.5
GRACE_FLOOR_MIN = 6

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 40)     # cc#855: derivatives run to 15:40; the outer session envelope


def _conn():
    return psycopg2.connect(_DB)


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


# ── cadence parsing ───────────────────────────────────────────────────────────────────────────
def cadence_window(cadence: str) -> Tuple[Optional[float], str, bool]:
    """Derive (expected_window_minutes, kind, market_gated) from a job's own cadence_human string.

    Returns window=None for kinds that have no periodic expectation at all (startup hooks, armed
    flags, event triggers). Those cannot be "late", so they are never STALE for time reasons —
    only for a failed last_status.

    The strings are the ones scheduler_master auto-captures from the dispatch expression, so this
    reads the real condition rather than a hand-maintained description that could drift.
    """
    c = (cadence or "").strip()
    low = c.lower()
    gated = bool(re.search(r"_is_market_hours|_is_cash_continuous|_is_session_live|_is_trading_day", c))

    if not c:
        return None, "unknown", gated
    if "on app startup" in low:
        return None, "startup", gated
    if "armed-flag-only" in low or "armed" in low:
        return None, "armed", gated
    if "event-triggered" in low:
        return None, "event", gated
    if "in-process while-loop" in low:
        return None, "in_process", gated

    # Calendar-locked first — these are the ones a naive minute-parse would badly misjudge.
    if "now.month in" in low:
        return 132000.0, "quarterly", gated          # ~92 days
    if "now.day ==" in low or "now.day >=" in low:
        return 46080.0, "monthly", gated             # ~32 days
    if "now.weekday() ==" in low:
        return 11520.0, "weekly", gated              # ~8 days

    # every tick (unconditional) — the scheduler loop runs once a minute
    if "every tick" in low:
        return 2.0, "per_tick", gated

    # m % N == 0  -> every N minutes. Take the SMALLEST modulus if several are present, because the
    # job fires on the union of its dispatch conditions, so the tightest one sets the expectation.
    mods = [int(x) for x in re.findall(r"m\s*%\s*(\d+)\s*==", c)]
    if mods:
        return float(min(mods)), "minutes", gated

    # h == X and m == Y -> once (or a few times) a day
    if re.search(r"h\s*==\s*\d+", c):
        return 1440.0, "daily", gated

    if "nightly" in low or "chained" in low:
        return 1440.0, "daily", gated

    return None, "unknown", gated


def _in_session(now_ist: datetime) -> bool:
    return now_ist.weekday() < 5 and MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE


def _minutes_since_session_end(now_ist: datetime) -> float:
    """How long the market has been shut. Used to forgive market-gated jobs out of hours."""
    if _in_session(now_ist):
        return 0.0
    d = now_ist
    # walk back to the most recent weekday close
    for _ in range(8):
        close_dt = d.replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute,
                             second=0, microsecond=0)
        if d.weekday() < 5 and now_ist >= close_dt:
            return max(0.0, (now_ist - close_dt).total_seconds() / 60.0)
        d = (d - __import__("datetime").timedelta(days=1)).replace(hour=23, minute=59)
    return 0.0


def badge_for(active: bool, last_status: Optional[str], age_min: Optional[float],
              cadence: str, now_ist: datetime) -> Dict[str, Any]:
    """LIVE / STALE / OFF — three states only, per the card.

    OFF   not active, or never run (NULL last_run_at). A model that has never run is OFF, never
          LIVE — that is the ENGINE_LIVENESS_RULE distinction between registered and running.
    STALE active but last_status is not ok, OR older than its own cadence window. Always states
          its own age, because "stale" without a number is not actionable.
    LIVE  active, ok, and inside its window.
    """
    if not active:
        return {"state": "OFF", "reason": "job is not active"}
    if age_min is None:
        return {"state": "OFF", "reason": "has never run"}

    window, kind, gated = cadence_window(cadence)

    if (last_status or "").lower() not in ("ok", "skipped"):
        return {"state": "STALE", "reason": f"last run reported '{last_status}'",
                "age_min": round(age_min, 1), "cadence_kind": kind}

    if window is None:
        # No periodic expectation (startup / armed / event). It cannot be late; it either ran
        # cleanly or it did not, and the branch above already caught 'did not'.
        return {"state": "LIVE", "reason": f"{kind} job, last run ok",
                "age_min": round(age_min, 1), "cadence_kind": kind}

    allowed = max(window * GRACE_MULT, GRACE_FLOOR_MIN)
    # THE MARKET-GATED FORGIVENESS. Out of hours, a session-only job is not expected to have run,
    # so the time it has spent waiting for the next open does not count against it.
    if gated and not _in_session(now_ist):
        allowed += _minutes_since_session_end(now_ist)

    if age_min > allowed:
        return {"state": "STALE", "reason": f"last ran {_ago(age_min)}",
                "age_min": round(age_min, 1), "expected_within_min": round(allowed, 1),
                "cadence_kind": kind}
    return {"state": "LIVE", "reason": f"last ran {_ago(age_min)}",
            "age_min": round(age_min, 1), "expected_within_min": round(allowed, 1),
            "cadence_kind": kind}


def _ago(m: Optional[float]) -> str:
    if m is None:
        return "never"
    if m < 1:
        return "just now"
    if m < 60:
        return f"{int(m)}m ago"
    if m < 1440:
        return f"{m/60:.0f}h ago"
    return f"{m/1440:.0f}d ago"


def build_status(cur, public_only: bool = True) -> Dict[str, Any]:
    """The launcher payload. READ-ONLY — this module never writes scheduler_master."""
    cur.execute("""
        SELECT r.job_name, r.display_name, r.description, r.route, r.sort_order, r.is_public,
               s.active, s.last_status, s.cadence_human,
               s.last_run_at,
               EXTRACT(EPOCH FROM (NOW() - s.last_run_at)) / 60.0 AS age_min
        FROM model_registry r
        LEFT JOIN scheduler_master s ON s.job_name = r.job_name
        WHERE (%s = false OR r.is_public = true)
        ORDER BY r.sort_order, r.display_name
    """, (public_only,))
    rows = cur.fetchall()

    now_ist = _ist_now()
    models, orphans = [], []
    for (job, name, desc, route, order, is_pub,
         active, last_status, cadence, last_run, age) in rows:
        if active is None:
            # Registry row with no scheduler_master job. Surfaced, never dropped — this is the
            # card's stop_condition ("a model with no job row is a liveness gap") as data.
            orphans.append({"job_name": job, "display_name": name, "route": route,
                            "reason": "no scheduler_master row for this job_name"})
            continue
        b = badge_for(bool(active), last_status, float(age) if age is not None else None,
                      cadence or "", now_ist)
        models.append({
            "job_name": job, "display_name": name, "description": desc, "route": route,
            "sort_order": order, "is_public": is_pub,
            "state": b["state"], "state_reason": b.get("reason"),
            "age_min": b.get("age_min"), "last_run_at": str(last_run) if last_run else None,
            "last_run_human": _ago(b.get("age_min")),
            "cadence": cadence, "cadence_kind": b.get("cadence_kind"),
            "expected_within_min": b.get("expected_within_min"),
        })

    counts = {"LIVE": 0, "STALE": 0, "OFF": 0}
    for m in models:
        counts[m["state"]] = counts.get(m["state"], 0) + 1
    return {
        "models": models, "count": len(models), "counts": counts,
        "orphans": orphans,
        "in_session": _in_session(now_ist),
        "as_of": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        "basis": ("enumerated from scheduler_master(active) JOIN model_registry(is_public) — "
                  "no model list in code (cc#860 V1 / SCHEDULER_MASTER_RULE 5700)"),
        "badge_rule": ("LIVE = active, last run ok, inside its OWN cadence window. "
                       "STALE = active but failed or overdue, age always stated. "
                       "OFF = inactive or never run. Registration alone is never LIVE "
                       "(ENGINE_LIVENESS_RULE 13829)."),
    }


@router.get("/api/models/status")
def models_status(public_only: bool = True):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_status(cur, public_only=public_only)
    except Exception as e:
        log.exception("model launcher status failed")
        return {"models": [], "count": 0, "counts": {}, "orphans": [],
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
