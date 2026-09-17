# cc#2152 — task-bookkeeping gap: nightly unfinished-with-commit alert + the room's "Shipped today" tab

Date: 17-Sep-2026. Files: `cc_queue_maintenance.py`, `room_endpoints.py`, `scorr_room.html`, this report.

## 1. The alert — `CC_TASKS_UNFINISHED_WITH_COMMIT`

Rides the existing 15-minute queue job (`bg_stale_claim_release`, an active `scheduler_master`
row) instead of adding a registry row. `cc_queue_maintenance.alert_unfinished_with_commit()` runs
after the release step commits, in its own transaction, so a failure there can never undo a release.

- The query, verbatim from the card:
  ```sql
  SELECT id, title, commit_sha, claimed_at FROM cc_tasks
   WHERE status = 'in_progress' AND commit_sha IS NOT NULL AND result IS NOT NULL
     AND finished_at IS NULL AND claimed_at < NOW() - (12 * INTERVAL '1 hour')
   ORDER BY claimed_at
  ```
- ONE `ops_log` row per IST day (category `alert`, title `CC_TASKS_UNFINISHED_WITH_COMMIT`),
  written by the first tick of the IST day that finds offenders; `details` lists them by id, title,
  commit_sha and claimed_at. Idempotence is a lookup of today's row, so a redeploy or a restart
  cannot double-write. No offenders → no row, one log line.
- Alert only. Nothing here touches a task row; closing stays Fable's diff + DB verification.
- Note on clocks: `ops_log.session_ts` is written with `NOW()` and stored naive in UTC (checked
  against the `ist` field other alerts carry), so the once-a-day test converts to IST before comparing.

Dry run on production (read-only) at 11:07 IST: one offender today — cc#1998 (claimed 11-Sep,
commit 28c7a90, result written, `finished_at` NULL). cc#2114, cc#2117 and cc#2139 were closed by
Fable this morning, so they are no longer offenders.

Unit test (fake cursor): written once with the offender list and cc 2152 in the details; skipped
when today's row exists; silent with no offenders; the release path survives an alert failure.

**First-run evidence:** the first 15-minute tick after this deploy runs the check; with cc#1998
open it should write today's row within a quarter hour of landing. The row (id, time, offender
list) is reported in the Fable Room when it appears; until then this is built-and-registered, not
live.

## 2. The room's Pushes tab → "Shipped today"

What the old tab was: `scorr_room.html` filtered messages by kind `push`, which
`room_endpoints.classify` derives from the message prefix (`PUSH …` / `P1 …`). It never read
`cc_tasks.status = 'pushed'`, so the card's reading ("a status unused since 06-Sep feeds it") was
one step off — but the founder's ask stands either way: the day's shipped cards as a list.

- New endpoint `GET /api/room/shipped` (one SELECT, read-only like the rest of the module):
  ```sql
  SELECT id, title, priority, commit_sha, finished_at FROM cc_tasks
   WHERE status = 'done'
     AND (finished_at AT TIME ZONE 'Asia/Kolkata')::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
   ORDER BY finished_at DESC, id DESC
  ```
  Returns `date_ist`, `count`, `tasks`, `repo_url`.
- The tab is labelled **Shipped today**, its pill shows the count, and it renders one row per
  card: id, title, commit link (7-char sha → GitHub commit), finished time in IST. Fetched
  separately from the feed; if it fails, the tab says so and the conversation is untouched.
- The `push` message kind and its sha links are kept on the other tabs.
- Legacy `status = 'pushed'` rows: none exist on production today (Fable had already re-closed
  them), so there was nothing to leave untouched.

Real rows today: 72 tasks done on 2026-09-17 IST (Fable's morning close-out batch at 09:27 plus
the cards landed since). Screenshot check (VISUAL_VERIFY_GATE_V1): the harness stubs the endpoint
with 12 of those 72 real rows and shows 12 rendered rows on desktop (1280) and phone (375), the
count pill reading 12, every sha linking to its commit, no horizontal overflow; the All tab still
shows its 3 messages with the push line's sha link. Screenshots looked at.

## For the live check

- `https://scorr.in/room` → tab "Shipped today" with today's count; click it → the list.
- `https://scorr.in/api/room/shipped` → `count` 72 (as of 11:10 IST), `date_ist` 2026-09-17.
- After the first tick post-deploy: `SELECT session_ts, details FROM ops_log WHERE title = 'CC_TASKS_UNFINISHED_WITH_COMMIT' ORDER BY id DESC LIMIT 1` → one row naming cc#1998.
