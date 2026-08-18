"""
room_endpoints.py — cc#1086 THE FABLE ROOM VIEWER
==================================================
    GET /room             -> scorr_room.html
    GET /api/room/feed    -> the thread, newest task first, oldest message first within a task

WHAT THIS IS. CC_COMMS_LOOP_V1 (session_log 24138) says `cc_task_logs` IS the meeting room: Fable
and CC talk through the table and the founder moderates. Until now watching that conversation meant
opening a SQL client. This is the window. Founder request 17-Aug: he wants to watch the working
loop rather than ask for a status report.

READ-ONLY, ABSOLUTELY. This module contains no INSERT, no UPDATE, no DELETE. The room is written by
CC and Fable; a browser is a spectator. There is no comment box and no reply field, and adding one
is a different trust boundary that needs its own card — not a small extension of this one.

ORDERING IS THE WHOLE READABILITY DECISION. Newest TASK first, because the founder wants to see
what is happening now. But OLDEST MESSAGE FIRST within a task, because a conversation has to read
downward — claim, push, finding, question, ruling — the way it actually happened. Sorting messages
newest-first would show every answer above its question. Pagination is BY TASK, not by row: a task
is the unit of meaning, and half a thread is worse than no thread.

KIND IS DERIVED, NEVER STORED. The prefixes are a writing convention CC and Fable follow by hand,
so the mapping lives in ONE function (`classify`) and a convention change has a single place to
fix. Deriving also means the room stays honest about history: a message written before a prefix
existed classifies as a note rather than being retro-labelled.

LANES follow the seat, not the string. Actor values in the wild are inconsistent — `claude_code`
and `CC` both exist, and `fable` arrives in more than one case — so they are normalised
case-insensitively HERE rather than by adding rows to the table. Writing to the room to tidy the
room would break the read-only rule on the first day.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("scorr.room")
router = APIRouter(tags=["room"])

_DB = os.getenv("DATABASE_URL", "")
_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorr_room.html")

# The repo the shas belong to. Kept as a constant rather than inlined so a fork or rename is one
# edit, and so nothing in the page template has to know about GitHub at all.
_REPO_URL = "https://github.com/AGQuant/quant_project"

# A sha as CC writes it in a push line: 7 to 40 hex characters. The lower bound is deliberately 7
# rather than 6 — a 6-char lower-case hex run matches ordinary words like "decade" and "accede",
# and a false sha rendered as a commit link is worse than a missed one.
_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# Ordered, because the first match wins and the order encodes precedence. STOPPED and QUESTION come
# before push: a message can carry a sha AND a stop, and the stop is the thing the founder needs to
# see. RECO is Fable's ruling. CLAIMED is the start of a thread.
_KINDS = (
    ("stopped",  re.compile(r"^\s*STOPPED\s*:", re.I)),
    ("question", re.compile(r"^\s*QUESTION\s*:", re.I)),
    ("reco",     re.compile(r"^\s*RECO\s*:", re.I)),
    ("push",     re.compile(r"^\s*(?:PUSH\b|P\d+\b)", re.I)),
    ("claim",    re.compile(r"^\s*(?:CLAIMED|Claimed|Started)\b")),
)

# Which lane a seat sits in. Everything not named here is centre — the scheduler, the killswitch,
# and any actor added later — so a new writer appears in the room immediately rather than vanishing
# because nobody updated a list.
_LANES = {
    "claude_code": "cc", "cc": "cc", "claude-code": "cc",
    "fable": "fable", "claude_ai": "fable", "claude-ai": "fable",
}


def _conn():
    return psycopg2.connect(_DB)


def classify(message: str) -> str:
    """The ONE place the prefix convention is read. See the module docstring."""
    m = message or ""
    for kind, rx in _KINDS:
        if rx.search(m):
            return kind
    return "note"


def lane(actor: str) -> str:
    """claude_code left, fable right, everything else centre. Case-insensitive by design."""
    return _LANES.get((actor or "").strip().lower(), "other")


def extract_sha(message: str) -> Optional[str]:
    """The commit sha from a push line, or None.

    Only looked for on a push, because a hex-looking token inside a paragraph of prose is far more
    likely to be a number than a commit. Restricting the search to the message HEAD (the part
    before the first sentence break) keeps a sha quoted mid-explanation from being promoted to the
    header of a different message.
    """
    head = (message or "").split(".")[0][:200]
    m = _SHA_RE.search(head)
    return m.group(1) if m else None


def _rows_to_thread(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group flat log rows into per-task threads, preserving the SQL ordering.

    The SQL already returns task-newest-first and message-oldest-within-task, so this walks once
    and never re-sorts. Re-sorting here would be a second ordering authority and the two would
    eventually disagree.
    """
    threads: List[Dict[str, Any]] = []
    index: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        tid = r["task_id"]
        t = index.get(tid)
        if t is None:
            t = {
                "task_id": tid,
                "title": r.get("title"),
                "status": r.get("status"),
                "priority": r.get("priority"),
                "messages": [],
            }
            index[tid] = t
            threads.append(t)
        if r.get("log_id") is None:
            continue          # the LEFT JOIN placeholder for a task with no messages yet
        msg = r.get("message") or ""
        kind = classify(msg)
        t["messages"].append({
            "id": r["log_id"],
            "actor": r.get("actor"),
            "lane": lane(r.get("actor")),
            "kind": kind,
            "sha": extract_sha(msg) if kind == "push" else None,
            "ts": r["ts"].isoformat() if r.get("ts") else None,
            "message": msg,
        })
    return threads


@router.get("/api/room/feed")
def room_feed(tasks: int = Query(10, ge=1, le=50)):
    """The room, as JSON. Pure read — this endpoint issues exactly one SELECT."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # The task window is chosen FIRST, in its own subquery, so "10 tasks" means ten
                # tasks. Applying LIMIT to the joined rows would cut a thread in half at the
                # boundary and show a conversation with its ending missing.
                cur.execute("""
                    WITH recent AS (
                        SELECT t.id, t.title, t.status, t.priority,
                               COALESCE(MAX(l.ts), t.created_at) AS last_activity
                          FROM cc_tasks t
                          LEFT JOIN cc_task_logs l ON l.task_id = t.id
                         GROUP BY t.id, t.title, t.status, t.priority, t.created_at
                         ORDER BY last_activity DESC NULLS LAST
                         LIMIT %s
                    )
                    SELECT r.id   AS task_id, r.title, r.status, r.priority,
                           l.id   AS log_id, l.actor, l.message, l.ts
                      FROM recent r
                      LEFT JOIN cc_task_logs l ON l.task_id = r.id
                     ORDER BY r.last_activity DESC, l.id ASC
                """, (tasks,))
                rows = cur.fetchall()
        threads = _rows_to_thread([dict(r) for r in rows])
        return {
            "threads": threads,
            "task_count": len(threads),
            "message_count": sum(len(t["messages"]) for t in threads),
            "repo_url": _REPO_URL,
        }
    except Exception as e:
        # NEVER a cached or partial room. A stale meeting room is a lie about what the team is
        # doing, so the failure is stated and the page renders the failure instead of content.
        log.exception("room feed failed")
        return JSONResponse(status_code=503,
                            content={"error": "The room is unreachable — %s" % str(e)[:200]})


@router.get("/room", response_class=HTMLResponse)
def room_page():
    """The viewer. Read-per-request, so a deploy serves the new file immediately."""
    try:
        with open(_PAGE, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif'>Fable Room unavailable</h3>"
            "<p style='font-family:sans-serif'>scorr_room.html is missing from the deploy.</p>",
            status_code=500,
        )
