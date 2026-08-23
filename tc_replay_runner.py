"""tc_replay_runner.py — cc#1220: the transport cc#1211 was missing.

WHY THIS EXISTS AT ALL. The cc#1211 replay engine has been built and deployed since 22-Aug and
has never run once, because the only way to start it was POST /api/admin/run-tc-replay and
scorr.in is egress-blocked from both the CC and the Fable seat. An engine nobody can start is not
live, and rule 9 will not let it be called done. MCP tools DO reach the app, so this card adds
transport and nothing else.

WRAPS, DOES NOT CHANGE. Every number still comes from tc_score_replay - score_all, sweep,
results_table, best_cells, bucket_breakdown, selfcheck, coverage. Not one line of the scoring or
the as-of loader is touched or re-implemented here. If a figure in the status payload disagrees
with the endpoint, the endpoint is right and this file has a bug.

IN-PROCESS, NOT OVER HTTP. The rest of mcp_dispatch calls the app back through BASE_URL, which
works but means the MCP layer depends on the public domain resolving from inside the container
and on ADMIN_TOKEN being current. The replay is the one job that must not fail for either reason,
so it imports and calls directly - the same choice cc#790 made for run_fundamentals_scrape, and
for the same stated reason: a rotated token must not be able to silently block the run.

A DAEMON THREAD, AND THE STATE IS REAL. The replay is roughly 100k card evaluations across five
sessions; it cannot hold an MCP request open. It runs on a background thread and start() returns
immediately. The busy flag is guarded by a lock and set BEFORE the thread starts, so two callers
racing on the same second cannot both get {started:true} - the second one is told the truth.

The thread is daemon: a deploy restarts the container and an in-flight replay dies with it. That
is deliberate and it is safe, because scoring upserts on (ts, symbol, bucket) and the sweep clears
each cell before refilling it, so the correct response to a killed run is simply to fire it again.
What must never happen is a half-filled table being read as a result, which is why status()
reports row counts and the coverage flags rather than a bare "ok".
"""

import threading
import traceback
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "phase": None,
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_SEQ = [0]


def _ist_now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _walk(phase):
    """The actual run. Every exit path clears `running` — a crashed thread that left the flag set
    would lock the tool out until the next deploy, which is a worse failure than the crash."""
    out = {}
    err = None
    try:
        import tc_score_replay as R
        if phase in ("score", "all"):
            out["ticks_scored"] = R.score_all()
        if phase in ("sweep", "all"):
            out["trades"] = R.sweep()
        # cc#1221: portfolio is DELIBERATELY NOT part of "all". It re-walks the stored ticks and
        # never re-scores, so it is cheap and independent — but folding it into `all` would mean a
        # routine re-score also silently rebuilt the capped book, and the two runs answer different
        # questions the founder asked separately. Ask for it by name.
        if phase == "portfolio":
            out["port_trades"] = R.portfolio()
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
        out["traceback"] = traceback.format_exc().splitlines()[-12:]
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = _ist_now_str()
            _STATE["result"] = out
            _STATE["error"] = err


def start(phase="all"):
    """Kick a replay. Returns immediately; poll status()."""
    phase = str(phase or "all").lower().strip()
    if phase not in ("all", "score", "sweep", "portfolio"):
        return {"error": "phase must be one of all, score, sweep, portfolio — got %r" % phase}
    with _LOCK:
        if _STATE["running"]:
            return {"busy": True, "phase": _STATE["phase"], "run_id": _STATE["run_id"],
                    "started_at": _STATE["started_at"],
                    "note": "a replay is already walking; poll tc_replay_status"}
        _SEQ[0] += 1
        run_id = "tcr-%s-%d" % (datetime.now(IST).strftime("%Y%m%d-%H%M%S"), _SEQ[0])
        _STATE.update(running=True, phase=phase, run_id=run_id,
                      started_at=_ist_now_str(), finished_at=None, result=None, error=None)
    threading.Thread(target=_walk, args=(phase,), name="cc1220-tc-replay", daemon=True).start()
    return {"started": True, "phase": phase, "run_id": run_id,
            "sessions": _sessions(),
            "note": "roughly 100k card evaluations; poll tc_replay_status for row counts. "
                    "Safe to fire again — scoring upserts on (ts, symbol, bucket) and the sweep "
                    "clears each cell before refilling it."}


def _sessions():
    try:
        import tc_score_replay as R
        return list(R.SESSIONS)
    except Exception:
        return None


def status(selfcheck=True):
    """Where the replay stands, from the DATABASE and not from the run state.

    The run state says what this process last did; the row counts say what actually exists. Both
    are reported because they can disagree — a container that restarted mid-run has no run state
    at all and would otherwise look like it had never started, while the ticks table holds
    whatever the dead run managed to write.
    """
    with _LOCK:
        run = dict(_STATE)

    out = {"run": run, "ticks": None, "trades": None}
    try:
        import tc_score_replay as R
        out["sessions"] = list(R.SESSIONS)
        out["merit_gate"] = {"avg": R.MERIT_AVG, "acc": R.MERIT_ACC}
        out["asof_limits"] = R.ASOF_LIMITS
        with R._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tc_score_replay_ticks")
            out["ticks"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM tc_score_replay_trades")
            out["trades"] = cur.fetchone()[0]
            # the two honesty flags, computed by the engine rather than restated here
            try:
                out["coverage"] = R.coverage(cur)
            except Exception as e:
                out["coverage"] = {"error": "%s: %s" % (type(e).__name__, e)}
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out

    # THE TABLE IS ONLY BUILT WHEN THERE IS SOMETHING IN IT. results_table() on an empty sweep
    # renders a grid of dashes, and a grid of dashes read quickly looks like a result rather than
    # an absence. Below the bar it says so in words instead.
    if out["trades"]:
        try:
            out["markdown"] = R.results_table()
            out["best"] = R.best_cells(2)
            out["breakdowns"] = [R.bucket_breakdown(c["threshold"], c["hold"]) for c in R.best_cells(2)]
        except Exception as e:
            out["markdown_error"] = "%s: %s" % (type(e).__name__, e)
    else:
        out["markdown"] = None
        out["note"] = ("no trades stored yet — the sweep has not produced a cell. "
                       "This is an ABSENCE, not a result of zero.")

    # cc#1221: the capped-book summary. None when portfolio has never run - an ABSENCE, reported
    # as one, rather than a block of zeros that reads like a book that traded nothing.
    try:
        out["portfolio"] = R.portfolio_summary()
    except Exception as e:
        out["portfolio"] = {"error": "%s: %s" % (type(e).__name__, e)}

    # The selfcheck is the only test that catches the as-of loader having drifted from the live
    # scorer, so it rides along with the status rather than waiting to be asked for separately.
    # It is allowed to fail on its own without taking the counts down with it.
    if selfcheck:
        try:
            out["selfcheck"] = R.selfcheck()
        except Exception as e:
            out["selfcheck"] = {"error": "%s: %s" % (type(e).__name__, e)}
    return out
