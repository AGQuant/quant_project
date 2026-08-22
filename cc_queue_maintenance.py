"""cc_queue_maintenance.py — cc#1189, CC_QUEUE_DRAIN_RULE_V1 point 4: stale claim release.

WHAT THIS IS FOR, in the founder's words rather than mine: two days of tasks left mid-way. A CC
session claims a cc_task, the session ends, and the row sits `in_progress` for ever. Nothing else
picks it up, because a later session reads `in_progress` as "somebody is on it" — and the somebody
was a process that no longer exists. The founder has been restarting me by hand to clear them.

THE RULE THIS ENFORCES (session_log 28971, point 4): an in_progress task with NO commit_sha whose
claim is older than 90 minutes is released back to `pending`. A fresh session then treats it as
ordinary queue work.

WHY 90 MINUTES AND NOT LESS. It has to be longer than a real task takes, or the job steals work
from a session that is still doing it. The long cards in this queue run 30-60 minutes end to end,
so 90 leaves headroom; the cost of waiting is one idle interval, and the cost of being too eager is
two sessions editing the same file.

THE ONE CONDITION THAT MATTERS MOST IS THE ONE THAT DOES NOTHING VISIBLE: commit_sha IS NULL.
A task that has already pushed is FINISHED work — releasing it would invite a second session to
redo a landed change, which is worse than leaving a stale row. The card says it outright ("tasks
with a commit_sha are never reset") and it is the first thing the SQL checks.

WHERE THIS RUNS, because the card gates on it. This is a plain module imported by scheduler.py and
dispatched from the app's own 15-minute tick. The Fyers feed worker is a SEPARATE Railway service
(`truthful-friendship`) whose redeploys are watched on `worker/**` plus two shared root modules;
this file is none of those, so nothing here can bounce the feed. The gate the card names —
"if the job can only live in a process that redeploys the feed worker, STOP" — does not fire.

READ-ONLY ON EVERYTHING EXCEPT THE TWO COLUMNS IT OWNS. It writes `status` and `claimed_at` on
rows it releases, and appends one cc_task_logs line per release. No DDL, no deletes, nothing else.
"""

import logging
import os

import psycopg

log = logging.getLogger("scorr.cc_queue")

_DB = os.getenv("DATABASE_URL", "")

# The claim age past which an in_progress row with no push is treated as abandoned.
STALE_CLAIM_MINUTES = 90

# A CTE, and the reason is a bug the first version shipped with — caught by RUNNING the statement
# against the real table rather than reading it.
#
# The obvious form is UPDATE ... SET claimed_at = NULL ... RETURNING id, title,
# EXTRACT(EPOCH FROM (now() - claimed_at))/60. It parses, it runs, it releases the right rows —
# and every claimed_min comes back NULL, because RETURNING sees the NEW row and the new claimed_at
# is the NULL we just wrote. The releases were correct and every log line would have read
# "released after None min", which is the kind of wrong that survives review: the job works, only
# its evidence is empty.
#
# So the age is measured in a CTE that reads the rows BEFORE the update touches them, the UPDATE
# joins to that set, and the final SELECT returns the pre-update values. Same rows, real numbers.
# cc#1189 amendment_v1_2 (session_log 29016, Fable 22-Aug): category='audit' rows are FABLE-OWNED
# STANDING LEDGERS. They are claimed in_progress for days by design and never carry a commit_sha,
# which is precisely the shape this job hunts. Without this clause the 90-minute rule would reset
# cc#1199 — an open audit ledger — back to pending every quarter of an hour, for ever.
#
# It is a category exclusion rather than an id list on purpose: naming cc#1199 would fix today and
# break again on the next ledger somebody opens.
_AUDIT_CATEGORY = "audit"

# The SAME predicate as the release, used read-only. It exists because of what this job could not
# tell me on 22-Aug: it had been recording last_status='skipped' every 15 minutes for hours while
# two rows sat plainly eligible, and _bg_stale_claim_release returns a bare _SKIPPED for TWO
# COMPLETELY DIFFERENT REASONS — the registry gate said no, or the query found nothing. From the
# outside those are the same word, so there was no way to tell which had happened without running
# the statement by hand. A job that cannot say why it did nothing is not observable, and that is
# its own defect regardless of which cause turns out to be the real one. Now every run records the
# candidate count, so "0 candidates" and "2 candidates, 0 released" stop looking identical.
_CANDIDATE_SQL = """
    SELECT COUNT(*) FROM cc_tasks
     WHERE status = 'in_progress'
       AND commit_sha IS NULL
       AND claimed_at IS NOT NULL
       AND claimed_at < now() - (%s * interval '1 minute')
       AND COALESCE(category, '') <> %s
       AND id <> ALL(%s)
"""

_RELEASE_SQL = """
    WITH stale AS (
        SELECT id, title,
               EXTRACT(EPOCH FROM (now() - claimed_at)) / 60.0 AS claimed_min
          FROM cc_tasks
         WHERE status = 'in_progress'
           AND commit_sha IS NULL
           AND claimed_at IS NOT NULL
           AND claimed_at < now() - (%s * interval '1 minute')
           AND COALESCE(category, '') <> %s
           AND id <> ALL(%s)
    ), released AS (
        UPDATE cc_tasks t
           SET status = 'pending', claimed_at = NULL
          FROM stale s
         WHERE t.id = s.id
     RETURNING t.id
    )
    SELECT s.id, s.title, s.claimed_min
      FROM stale s
      JOIN released r ON r.id = s.id
     ORDER BY s.claimed_min DESC
"""


def release_stale_claims(skip_ids=None, minutes=STALE_CLAIM_MINUTES, conn=None):
    """Release abandoned claims. Returns a list of {id, title, claimed_min} for what was released.

    `skip_ids` is the escape hatch the card asks for on the FIRST manual run: the task a live CC
    session is working right now must not be yanked out from under it. It is a parameter and not a
    lookup because there is no reliable way for this job to ask "is a session alive" — the whole
    problem is that dead sessions leave no trace. The scheduled tick passes nothing, which is
    correct: by then the 90-minute age test is the only signal available, and it is the rule.

    `claimed_at IS NOT NULL` is stated even though the age comparison would already exclude NULLs.
    A NULL claimed_at on an in_progress row means something set the status without claiming, and
    that is a different fault — this job should not quietly adopt it.
    """
    skip = list(skip_ids or [])
    own = conn is None
    c = psycopg.connect(_DB) if own else conn
    try:
        with c.cursor() as cur:
            # COUNT FIRST, and it is not a nicety. See _CANDIDATE_SQL for what this job could not
            # tell anyone on 22-Aug. The count is taken with the identical predicate, so
            # candidates > 0 with released == 0 is a real contradiction the next reader can act
            # on rather than a silence they have to reproduce by hand.
            cur.execute(_CANDIDATE_SQL, (minutes, _AUDIT_CATEGORY, skip))
            candidates = cur.fetchone()[0] or 0
            cur.execute(_RELEASE_SQL, (minutes, _AUDIT_CATEGORY, skip))
            # The None guard is not defensive padding — it is what the first version needed and
            # did not have. If the age ever comes back NULL again the job must still release the
            # row and still write a log line, saying "unknown" rather than dying on float(None)
            # and leaving the task released with no record of why.
            released = [{"id": r[0], "title": r[1],
                         "claimed_min": (round(float(r[2]), 1) if r[2] is not None else None)}
                        for r in cur.fetchall()]
            for r in released:
                # actor='scheduler', per the card. The message names the age so the next reader can
                # tell a just-over-the-line release from a two-day-old orphan without a query.
                age = ("%.0f min" % r["claimed_min"]) if r["claimed_min"] is not None else "unknown age"
                cur.execute(
                    "INSERT INTO cc_task_logs (task_id, actor, message) VALUES (%s, %s, %s)",
                    (r["id"], "scheduler",
                     "stale claim released after %s (CC_QUEUE_DRAIN_RULE_V1, session_log 28971 "
                     "point 4) — in_progress with no commit_sha; back to pending for the next "
                     "session to claim" % age))
        if own:
            c.commit()
        # EVERY run says what it saw, including the quiet ones. A job that only logs when it acts
        # is indistinguishable from a job that is not running at all — which is exactly the state
        # this one was in while it recorded 'skipped' every quarter hour.
        if released:
            log.info("cc_queue: %d candidate(s), released %d: %s", candidates, len(released),
                     ", ".join("cc#%s" % r["id"] for r in released))
        elif candidates:
            log.error("cc_queue: %d candidate(s) matched the stale predicate but NOTHING was "
                      "released — the release statement and the count disagree, which should be "
                      "impossible on one connection. Investigate before trusting this job.",
                      candidates)
        else:
            log.info("cc_queue: 0 stale candidates (audit ledgers excluded) — nothing to release")
        return released
    finally:
        if own:
            c.close()
