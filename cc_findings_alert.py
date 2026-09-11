"""cc_findings_alert.py — cc#1971, DIAG_FINDINGS_SURFACE_V1 (session_log 43650): every FINDING row in
the Fable Room pings the founder's Telegram.

WHAT THIS IS FOR, in the founder's words: "CC did the diagnose tasks but they never came in front of
me in time." The standing order (43650) makes CC post one row per finished FINDINGS card to the
Room thread (cc_task_logs, task_id 1199, level 'finding'). Fable cannot receive a push; the founder
can. This module is the alert half: the same five-line summary goes to the founder's Telegram.

THE SENDER IS REUSED, NOT REBUILT. v10_st_ema.telegram_alert(msg) is the one Telegram client the
platform has — the V10 engine's entry alerts, the feed watchdogs (scheduler._alert_telegram) and
feed_guardian all go through it. It is a plain function: text in, {"sent": bool, "reason"?: str}
out; the bot token and chat id come from the environment (V10_TELEGRAM_BOT_TOKEN / BOT_TOKEN,
V10_TELEGRAM_CHAT_ID / CHAT_ID) — the founder's own chat, the same one the V10 alerts reach. No
second client is written here.

THE HOOK IS A POLL, NOT A TRIGGER. CC writes the Room straight through SQL (the run_sql tool);
there is no application-side writer function to hook, and a DB trigger with a network side effect
is against house practice. So this runs from the app scheduler every 5 minutes (scheduler.py
_bg_findings_alert, registry-gated on scheduler_master 'bg_findings_alert'), reads the finding rows
that have no sent marker, sends each once and records the marker.

THE SENT MARKER IS A SIDECAR, NEVER AN ALTER. cc_task_logs is not altered (MAINTENANCE_LOCK_RULE);
cc_finding_alerts (CREATE TABLE IF NOT EXISTS, which is permitted) holds one row per finding
log id with the send time and the sender's response — the first-run evidence lives there too.

SEND ONCE, AND NEVER LOOP ON A DEAD CHANNEL. A row is marked only after a successful send. When the
sender says the environment is not set, nothing is marked and the tick returns a skip (None, the
one skip signal this module has), so the rows wait for the channel instead of being silently marked
sent. A per-row send failure is logged and
retried next tick; a poison row cannot block the others because every row commits on its own.

READ-ONLY on cc_task_logs. Writes only its own sidecar table.
"""
import json
import logging
import os
from typing import Optional

import psycopg

# cc#1971 close-out: set once the first time we notice the Telegram leg is parked, so a parked
# channel is reported ONCE per process instead of every 5-minute tick (no retry storm).
_PARKED_LOGGED = False

log = logging.getLogger("scorr.cc_findings")

_DB = os.getenv("DATABASE_URL", "")
ROOM_TASK_ID = 1199
BATCH = 20               # per tick; the poll is every 5 minutes, so a backlog drains in a few ticks
TG_MAX = 3900            # Telegram sendMessage caps text at 4096 chars; leave room for the header

_DDL = """
CREATE TABLE IF NOT EXISTS cc_finding_alerts (
    log_id    BIGINT PRIMARY KEY,
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response  JSONB
);
"""


def _conn():
    return psycopg.connect(_DB)


def _send(text: str) -> dict:
    """The ONE sender. Imported lazily so this module stays importable in a context without the
    V10 module's dependencies; any exception becomes a not-sent result, never a raise."""
    try:
        import v10_st_ema
        r = v10_st_ema.telegram_alert(text)
        return r if isinstance(r, dict) else {"sent": bool(r)}
    except Exception as e:
        return {"sent": False, "reason": "sender raised: %s" % e}


def _format(row_id: int, ts, message: str) -> str:
    stamp = ts.strftime("%d %b %H:%M") if ts is not None else ""
    head = "Scorr · Fable Room finding (%s, log %s)\n" % (stamp, row_id)
    body = (message or "").strip()
    limit = TG_MAX - len(head)
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return head + body


def pending(cur, limit: int = BATCH):
    cur.execute("""SELECT l.id, l.ts, l.message
                   FROM cc_task_logs l
                   LEFT JOIN cc_finding_alerts a ON a.log_id = l.id
                   WHERE l.task_id = %s AND l.level = 'finding' AND a.log_id IS NULL
                   ORDER BY l.id ASC
                   LIMIT %s""", (ROOM_TASK_ID, limit))
    return cur.fetchall()


def send_unsent_findings(sender=None) -> Optional[dict]:
    """Poll → send → mark. Returns None when there was nothing to DO OR NOTHING TO DO IT WITH —
    no unsent rows, or the channel is parked — and the scheduler records a skip. Otherwise the
    shape is exactly {"sent": n, "failed": m, "ids": [...]}, all three keys always present,
    because the caller reads all three. `sender` is injectable for tests."""
    send = sender or _send
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        with conn.cursor() as cur:
            rows = pending(cur)
        if not rows:
            return None
        sent, failed, ids = 0, 0, []
        for row_id, ts, message in rows:
            resp = send(_format(row_id, ts, message))
            if resp.get("sent"):
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO cc_finding_alerts (log_id, response)
                                   VALUES (%s, %s::jsonb) ON CONFLICT (log_id) DO NOTHING""",
                                (row_id, json.dumps(resp, default=str)))
                conn.commit()
                sent += 1
                ids.append(row_id)
                continue
            reason = str(resp.get("reason") or "sender returned not-sent (HTTP not ok)")
            # cc#1971 close-out (founder 10-Sep 22:00): the Telegram leg is PARKED -- he declined to
            # set the env vars. A parked channel is a DECISION, not a fault, so it must not raise,
            # must not count as a failed send, and must not write an ops_log row on every tick. It is
            # logged ONCE per process and the tick returns a skip. Nothing is marked sent, so the
            # rows wait for the channel exactly as before. The moment V10_TELEGRAM_BOT_TOKEN and
            # V10_TELEGRAM_CHAT_ID exist, v10_st_ema.telegram_alert() stops returning this reason and
            # the ping starts on its own -- no code change here, which is why this card can close.
            if "env not set" in reason:
                global _PARKED_LOGGED
                if not _PARKED_LOGGED:
                    log.info("cc_findings: telegram PARKED (env not set); %d finding row(s) waiting. "
                             "Set V10_TELEGRAM_BOT_TOKEN + V10_TELEGRAM_CHAT_ID to start the ping.",
                             len(rows))
                    _PARKED_LOGGED = True
                # RETURN None, NOT A DICT. This function has exactly one skip signal and it is None
                # (see the docstring); the scheduler turns that into _SKIPPED. c600cbf returned a
                # {"skipped": ...} dict here instead, which is truthy, so the caller fell through to
                # its success log line and raised KeyError('failed') -- the job then recorded
                # last_status='error' every 5 minutes, which is the exact noise this branch was
                # written to stop. Caught on the 11-Sep 07:05 IST tick. The count the dict carried is
                # already in the one-time log line above, where a parked channel belongs.
                return None
            failed += 1
            log.warning("cc_findings: finding log %s not sent: %s", row_id, reason)
            # a failed send is written where it can be READ (ops_log), not only to a Railway log line
            try:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                                   VALUES (CURRENT_DATE, NOW(), 'cc_findings', 'telegram send failed', %s::jsonb)""",
                                (json.dumps({"log_id": row_id, "reason": reason, "response": resp}, default=str),))
                conn.commit()
            except Exception as e:
                log.warning("cc_findings: ops_log write failed: %s", e)
        if sent == 0:
            # nothing went out on a LIVE channel: say so as an error, never as an 'ok' run
            # (cc#526 lesson). The parked case returned above and never reaches this line.
            raise RuntimeError("cc_findings: %d pending, 0 sent (%s)" % (failed, reason))
        return {"sent": sent, "failed": failed, "ids": ids}
