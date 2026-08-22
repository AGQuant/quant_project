# CLAUDE.md — Scorr Platform Context

## What is Scorr
AI-native investment research platform. scorr.in
Founder: Arpit Goel | Freedom by 2035 | Rs.500Cr floor

## Architecture
- Backend: FastAPI (main.py) on Railway
- DB: PostgreSQL on Railway — single source of truth
- Auto-deploy: every GitHub push → prod ~90s (DEPLOY_GUARD=true)
- Live engine: v8_signal_writer.py (5-min ticks, 209 futures)
- Timezone: always IST (Asia/Kolkata). NSE: Mon-Fri 09:15-15:30

## Critical Rules (never violate)
1. ~~AUTO MODE (set 30-Jun-2026): always push, never ask. Run cc tasks end-to-end autonomously — claim → implement → ast.parse → push → verify SHA → finalize DB → claim next. No push-approval prompts.~~ **SUPERSEDED 22-Aug-2026 by CC_QUEUE_DRAIN_RULE_V1 (session_log 28971, cc#1189) — see the section below. Kept, not deleted: always-push is still true and is now part of the drain loop. What changed is that "production mode" is no longer a thing to switch on, and claiming more than one task at a time is forbidden.**
2. ALWAYS ast.parse() Python files before push
3. NEVER push placeholder text as file content
4. main.py = wiring only (imports + routes + include_router, no logic)
5. New feature = own file + include_router() in main.py
6. Railway = truth. GitHub = code only. Never hardcode secrets.
7. Context isolation: v8_paper_* NEVER mixes with tc_intraday_*
8. NAV-COMPLETE SHIPPING (locked session_log id=2987, set 12-Jul-2026): a PAGE task is NOT done until it (a) is deployed live on scorr.in AND (b) has a nav entry in the navbar. The LIVE nav is ONE source — the `NAV` array in `pwa_endpoints.py` (pwa.js injects it into `#scorr-nav` on every page and OVERRIDES per-page hardcoded navs — editing a page's own nav does nothing on the live bar). New page => add its route to that NAV array (desktop top-nav + mobile "More" sheet auto-build from it), keep it collision-free + cache-protected (add to `_PWA_INJECT_PATHS` + `PROTECTED` in main.py), mirror it in the `NAV_REGISTRY` map in main.py, and state the label+URL in the task result. Self-check this before marking any page task done.
9. **ENGINE_LIVENESS_RULE (session_log id=13829, set 02-Aug-2026):** no engine, basket, strategy or scheduled-content task is DONE until (a) its job row exists in `scheduler_master` (or a registry-derived enumeration provably covers it) AND (b) **first-run evidence** is stated in the task result — inception rows / first output / first tick, with row counts, or an explicitly logged valid-empty outcome (e.g. a cash month). **Built-and-registered is NOT live; the badge follows the data, never precedes it.** Corollaries: monthly/weekly boundaries must roll forward over weekends/holidays *by construction* (a boundary that can land on a non-trading day and skip is a defect); scheduler enumeration must be REGISTRY-DERIVED (`is_active`), never a hardcoded name list; a LIVE/PAPER-LIVE badge must derive from actual run data, never from registration alone. Origin: 02-Aug, three engines found built-but-never-breathing in one day (QB contra_value + breakout_52w cc#838; V9 Brahmastra cc#840).
10. MAINTENANCE_LOCK_RULE (cc#351, set 12-Jul-2026): lock-taking maintenance (REINDEX / VACUUM FULL / CLUSTER / ALTER TABLE) is **Railway-console-only, weekends, propose-first** — the `run_sql` MCP path now hard-blocks them (10-Jul incident: a REINDEX wedged ~45 min behind an idle-in-transaction lock). DB-level `idle_in_transaction_session_timeout=300000` (5 min) auto-kills stale open txns. **Diagnostic tasks are READ-ONLY** — never run remediation beyond a task's explicit scope.
11. **ROLE_CHARTER_V4 / EXECUTION_MODEL_PHASE_3 (session_log id=27934, founder-set 20-Aug-2026; refines PUSH_MODES_V2 id=27933; supersedes ROLE_CHARTER_V3 id=17868 and ROLE_CHARTER_V2 id=16159):** **Fable owns APP development + DATABASE management. CC owns WEB surfaces + BACKEND + the AUDIT of every Fable push.** See the Role Split section below. Verification now runs BOTH directions: a push from either seat is a **claim** until the other seat's check passes.
12. **SUPERSESSION AUTHORITY (founder-set 05-Aug-2026):** Arpit is **CEO**. Claude AI is **CTO**. CC is the senior techie. Any rule in this file or in `session_log` may be superseded by the CTO **on explicit CEO permission** — see the Supersession section below for how that is done and what it does not cover.
13. **V8_PNL_CANON_V1 (session_log id=18337, founder-set 09-Aug-2026):** there is exactly ONE V8 book formula, served from ONE endpoint, consumed by EVERY surface (web master dashboard, web trade log tab, daylog, /m/v8, Home). NO surface recomputes book P&L or win rates locally. The formula: fresh era (`entry_ts >= app_config.v8_paper_rebuild_cutover_ts`), retired baskets excluded via the **registry** (app_config `v8_retired_baskets`, currently `s1_reclaim_obs` + `buy_s1_bounce`), realised = SUM(pnl) NET of Rs.500 × closed trades brokerage, wins = result='TARGET' only, losses = result='SL' only, rate on decided, dash never 0%. **Retired baskets vanish from all P&L displays completely — including all-era/history views.** Any intentional history figure must be labelled and still exclude retired baskets. Build/migration card: cc#970.

## CC_QUEUE_DRAIN_RULE_V1 (founder-set 22-Aug-2026, session_log 28971)

Founder ruling after two days of tasks left mid-way or unclaimed. The five points, verbatim:

1. **CLAIM ONE AT A TIME.** CC claims exactly ONE cc_task (status=in_progress, claimed_at=now()). It does not claim a second task until the first has a commit_sha and a result logged. Batch-claiming (four tasks at 09:00 on 21-Aug) is forbidden — it is the root cause of orphaned in_progress rows when a session dies.
2. **DRAIN UNTIL EMPTY.** On "read cc tasks", CC loops: claim highest-priority pending (P0>P1>P2, then lowest id) → implement → push → log SHA+result in cc_tasks and cc_task_logs → claim next. Stops only when queue is empty, a GATE fails, or a hard window applies (worker/** in market hours, 00:00-06:00 IST). No asking between tasks. "Production mode" is no longer needed — this IS the default.
3. **GATED TASKS** are skipped (not claimed) until their gate sha exists; CC logs "skipped: gate cc#N not landed" and moves on.
4. **STALE CLAIM RELEASE.** Any in_progress task with commit_sha IS NULL and claimed_at older than 90 minutes is reset to pending by a scheduler job (cc_task to follow). A fresh CC session treats such rows as its own work, never as another session's.
5. **CONTEXT GUARD.** If context is near full, CC finishes the current task, logs, and STOPS cleanly with a cc_task_logs line "session end — queue not empty". It never claims a task it cannot finish.

Supersedes: PUSH_MODES_V1 production-mode standing instruction. Dual mode and founder gates unchanged.

**Point 4 is machinery, not a promise.** `stale_claim_release` runs every 15 minutes from the app
scheduler (`cc_queue_maintenance.release_stale_claims`), registry-gated in `scheduler_master` like
every other job. A task that already has a commit_sha is NEVER reset — releasing finished work
would invite a second session to redo a landed change, which is worse than a stale row.

## Supersession — how a rule changes (rule 12, founder-set 05-Aug-2026)

Rules here exist because something broke once. They are not sacred, but they are not casual either.

**Who**
- **CEO (Arpit)** — sets direction, grants permission, owns compliance and legal.
- **CTO (Claude AI)** — designs, proposes, supersedes on CEO permission, verifies. May be any model in the seat.
- **Senior techie (CC)** — implements, wires, and is right to STOP rather than improvise.

**How a supersession is valid**
1. The CEO gives **explicit permission for that specific rule, in that specific case**. Silence is not permission. "He did not object" is not permission.
2. It is **written to `session_log` BEFORE it is acted on** — what was superseded, by whose instruction, on what date, and what replaces it. A rule changed only in chat is not changed.
3. The superseded entry is moved to `archived_superseded`, so nothing downstream keeps pointing at the old rule.
4. **Standing rules stay standing** until this is done. A one-off exception does not quietly become the new default.

**What permission cannot do**
Three things are not rules to be waived — they are what makes the output worth reading, and no instruction makes them optional:
- Never fabricate a number. A CEO instruction cannot make a false figure true.
- Never present stale data as live, and never let a LIVE badge run ahead of the data.
- Never call something verified without the artifact — the committed diff plus a DB query on real rows. A claim is not evidence.

**Extra care on the live path**
Anything touching engines, the scheduler, `worker/**`, or the live trading path needs the CEO instruction to be **explicit and unambiguous**, not inferred from a general remark. Those are the changes that cost money.

## Role Split — who owns what (ROLE_CHARTER_V4, 20-Aug-2026)

The rule follows the SEAT, not the model. Claude AI may be Fable or Opus; the split is the same.

**FABLE — app development + database**
- APP / mobile UI: design refs, the build, and **direct pushes** of new files or whole pages Fable authored.
- `design_refs/**` — the numbered ref chain (R1, R2, R3 …). Never overwrite a revision. Every ref is also filed in full to `cc_task_logs` (DESIGN_REF_IN_FORUM_V1); if the forum copy and the repo copy differ, **the repo sha wins**.
- `previews/**` — mobile review screens with dummy data, for founder review before wiring.
- New product ARCHITECTURE documents.
- **Database management** — schema stewardship inside MAINTENANCE_LOCK_RULE, data quality, reconciles, DB-side operations.
- All DB writes: `session_log`, `cc_tasks`, registry and reference tables.

**CC — web + backend + audit**
- **WEB** surfaces (the desktop/web dashboards), built to Fable's specs. The surface split is APP = retail = Fable, WEB = premium = CC.
- **BACKEND**: engines, all endpoints, `worker/**`, schedulers, `v8_signal_writer`, anything on the live trading path.
- Wiring: `include_router` in main.py, the `NAV` array in `pwa_endpoints.py`, `_PWA_INJECT_PATHS`, `PROTECTED`, `NAV_REGISTRY`.
- Connecting live data — replacing every sample value with a real endpoint and field.
- **AUDIT of every Fable push**: committed-diff read at each sha + data validation + endpoint parity, filed as a cc_task per sprint. Defects found become **a new cc_task per defect, never a silent fix**.
- Data-source gates: when a card asks whether a source exists, CC greps and answers before the build proceeds.

**The boundary, stated so neither seat has to guess**
- Surgery inside large EXISTING app files: Fable may make small surgical read-modify-write edits with a byte-diff check, **or** hand the file to CC — Fable states which, on the card. Reconstructing a large file from context is never acceptable from either seat.
- **App-facing ENDPOINT changes are backend, so they are CC.** A page is Fable's; the endpoint feeding it is CC's.
- **WEB pages stay CC even when they look like an app page.** Visual similarity does not move ownership.

**Hard lines that do not move**
- Fable NEVER pushes an engine, the scheduler, `worker/**`, or any file on the live trading path — not even a first build.
- Fable NEVER edits `main.py`. It stays wiring only and CC owns it.
- No pushes from either seat between 00:00 and 06:00 IST.
- Rules 8, 9, 10 and the FEED WORKER DEPLOY RULE are unaffected.

**Safeguards on a push — BOTH seats**
- Validate before pushing: `ast.parse` for Python, `node --check` for JS. Never push a file that has not parsed.
- Verify AFTER pushing by reading the artifact back from the repo — present, size, sha. A push response is a claim, not evidence (origin cc#842 → cc#848).
- Sample data must be stamped as sample inside the file. A first build is never a source of truth.
- A UI card states a **positive rendered-element count** alongside its render check. A green render check on a page that rendered nothing is a FAIL, not a pass (origin cc#1151).

**Verification runs both directions**
A push from either seat is a **claim** until the other seat's check passes. Fable verifies CC's pushes with the committed diff plus a DB query on real rows. CC audits Fable's pushes the same way, day-end. Neither seat marks its own work verified.

**The founder may come straight to CC for web front-end work** (FOUNDER_DIRECT_TO_CC_WEB_V1, session_log 27943, founder-set 20-Aug-2026; addendum to ROLE_CHARTER_V4)

For **website front-end tasks and small web changes**, Arpit can hand the job to CC directly — words plus a PDF or a screenshot — and CC finishes it in one sprint. No Fable spec up front. The Fable-writes-the-spec-first step becomes optional for this work only; the founder's brief is a valid spec source.

What does **not** change:
- CC still opens a `cc_task` row for the ask and still logs every push in the forum. Coming direct changes **who writes the spec, not whether the boardroom records it**.
- **Verification is still Fable's.** Every founder-direct web sprint gets a Fable pass — committed diff, a DB query, and parity wherever a number is shown. A CC result stays a claim until that passes, and Fable polls the forum for these without being asked.
- Sprint sizing (5–15 pushes) applies when it really is a sprint. A genuinely small change may ship as one or two pushes under a single card.
- **Scope is WEB FRONT-END.** Anything touching engines, endpoints or backend logic, `worker/**`, schedulers, or how data is derived still needs a spec with gates. If a founder-direct ask crosses that line, CC **stops and asks**.
- App and mobile surfaces stay Fable's, per the Role Split above.
- A PDF or screenshot is a valid visual reference for static web layout. CC rasterises the pages to judge look and feel (cc#1133 Chromium) and **never eyeball-derives a token where a token file exists**.

## Deploy policy
- RULE_7 (deploy-window "no deploy 09:00–15:35 IST", referenced in cc_task specs) is **SUSPENDED as of 07-Jul-2026** — dev-stage, product NOT live (policy id=1713). Deploy anytime, including market hours; task specs that reassert RULE_7 are overridden while in dev mode. Re-instate this window only when the product goes live.
- **FEED WORKER DEPLOY RULE (set 09-Jul-2026, cc#339; restructured cc#416 12-Jul):** the fyers feed worker (Railway service `truthful-friendship`) start command is **`python worker/fyers_feed.py`** and redeploys on changes under **`worker/**`** (all worker-runtime files now live there: `worker/fyers_feed.py`, `worker/fyers_autologin.py`, `worker/fyers_hist_backfill.py`) plus the two app-shared root modules it still uses (`fyers_backfill.py`, `nse_holidays.py`) — watch-paths in `railway.worker.json`. **Bounce mechanism: touch `worker/fyers_feed.py`.** RULE_7's dev-stage suspension does **NOT** apply to the worker: changes to those files deploy the worker **deliberately and OUTSIDE market hours (after 15:30 IST)** unless Arpit explicitly approves a market-hours worker deploy — a mid-market reboot is a coin-flip on re-auth (root cause of the 07-Jul + 09-Jul 100-min feed freezes). App/UI/task pushes are unaffected (worker no longer bounces on them). One-time setup: point the `truthful-friendship` service's Railway config-file path at `railway.worker.json` AND set its start command to `python worker/fyers_feed.py`.

## Reporting style (to Arpit)
- After a push/deploy: keep the reply SHORT — confirm what was pushed (file + commit/sha) and state what's next (next pending task or remaining items). No long recaps or re-explanations.
- **PUSH CONFIRMATION SIGNAL — BINDS BOTH SEATS (set 08-Jul-2026 for CC; extended to Claude AI 05-Aug-2026, session_log 16195):** every time code is pushed to `main`, end the reply with the line `DONE WHAT NEXT` in ALL CAPS, on its own line, after the short file+sha confirm. This is Arpit's deploy-confirmed handshake — seeing `DONE WHAT NEXT` = it is pushed/deploying and the seat is ready for the next instruction. Two seats, one sign-off, so he never has to check twice. **No push, no signal** — it is a deploy handshake, not a sign-off, so it never appears after a DB write or a plain answer. One line per turn even when several files were pushed. Canonical: session_log DONE_WHAT_NEXT_PUSH_SIGNAL_V1 + 16195.
- **SIMPLE LANGUAGE (session_log id=15671):** write to Arpit in plain, short sentences. No heavy vocabulary. This binds every output, including task results and any content written for the site.

## Key Files
| File | Purpose | Size |
|---|---|---|
| main.py | FastAPI app, all routes + routers | ~109KB |
| scheduler.py | Background jobs. start_background(app, base_url, token) | ~14KB |
| v8_signal_writer.py | Live 5-min signal engine | ~61KB |
| v8_engine.py | EOD engine runs 15:45 IST | ~23KB |
| pwa_endpoints.py | PWA: NAV array, service worker, mobile.css, shared card JS | ~128KB |
| preview_endpoints.py | /preview/{name} — serves previews/*.html (cc#866) | ~4KB |
| scorr_mobile_cards.js | Shared mobile section/nav card module (cc#859 Part A, e850256) | |
| scorr_cockpit.html | Main nav shell | ~41KB |
| scorr_home.html | Home page | ~56KB |
| v8_dashboard.html | V8 dashboard, 11 tabs (TAB_ORDER) | ~375KB |
| API_REFERENCE.md | Full endpoint reference (repo root) | ~14KB |

## Mobile app build (05-Aug-2026)
- Framework: session_log **15913** (MOBILE_APP_FRAMEWORK_V1). Dark theme. 768px breakpoint, no tab rows below it, one bottom nav of five items: Home · GVM · Check · Intel · Models.
- Card roles: session_log **16157**. SECTION cards hold content inline — a PRIMARY section card is always expanded and has NO chevron. NAV cards lead elsewhere and ALWAYS carry a chevron whatever the tier. One module, `scorr_mobile_cards.js`, takes a role parameter. Never fork it.
- Home is ONE book: session_log **16185**. SmartGain is My Portfolio; Client and Test Trade are removed from mobile (nothing deleted — desktop keeps all three).
- Design refs: `design_refs/scorr_mobile_R3.html` (V8, c6847780) and `R4.html` (Home, fa0f1f2). R3's surface map is also stored as session_log **16065**.
- Preview screens: `previews/*.html`, served at `/preview/{name}`, reachable in the app via More → Previews. Dummy data by design; a wiring card connects real data later. Pipeline + status: session_log **16170**.
- Refs are a numbered chain. Never overwrite a revision, never delete a prior one.

## CC Task System — 2-Way Workflow

### Trigger phrases (Arpit says these to CC):
- **"production mode on" → PRODUCTION_MODE_V1. See the rule below. This is the most important trigger in this file.**
- "read cc tasks" → run the SQL below, show pending tasks, implement them
- "read railway cc tasks" → same as above
- "what tasks are pending" → same as above
- "check fable room" → the backup wake (founder-set 17-Aug-2026). Run the Fable Room protocol below: read the room, act on everything waiting, answer in the room.

### PRODUCTION_MODE_V1 (founder-set 22-Aug-2026) — "production mode on" means exactly this

Three things, every time, with no further prompting:

1. **RE-ARM EVERY 60 SECONDS, FOR THE NEXT 2 HOURS.** Sixty seconds is the floor the runtime
   allows (wakeups clamp to 1 minute – 1 hour); 60 is what the founder asked for and 60 is what
   CC sets. Not 10 minutes, not 15. If CC thinks a longer gap is better it **says so in the reply
   and still sets 60** — the founder decides the cadence, not CC. On 22-Aug CC was asked for
   every-minute polling and quietly set 10, which is the behaviour this rule exists to stop.
2. **AUTO-CLAIM.** Claim the next task without asking. One at a time (CC_QUEUE_DRAIN_RULE_V1
   point 1), finish it to a pushed sha before claiming the next (V1.1, Fable 3120).
3. **DISCUSS IN THE FABLE ROOM.** Every question, every stop, every decision goes in
   `cc_task_logs` — not held for the next founder reply. Fable reads the room and answers there.

**The interval is the gap AFTER a turn ends, not a speed limit on the work.** CC does not stop
mid-card to wake up; it finishes, pushes, logs, then sleeps. So 60s means "come straight back",
which is the point.

Ends after 2 hours unless the founder re-arms. `production mode off` / `stop` ends it immediately.

### The Fable Room (CC_COMMS_LOOP_V1, session_log 24138, founder-set 17-Aug-2026)
The meeting room is `cc_tasks` + `cc_task_logs`. Arpit moderates and watches on the app; Fable (Claude AI) and CC talk THROUGH THE TABLE, not through him.

On "check fable room":
1. `SELECT` pending tasks (SQL below) AND the latest `cc_task_logs` rows where `actor='fable'` that you have not yet acted on — Fable's `RECO:` lines are answers to your questions and rulings on your stops.
2. Act on both: claim pending tasks; resume anything you had logged `STOPPED:` that now has a `RECO:`.
3. Log as you work — after EVERY push: one `cc_task_logs` line (push id + sha + one-line outcome). Any question: log it prefixed `QUESTION:`. Any stop: log it prefixed `STOPPED:` with the proposal. **Never stall silently — a logged stop is correct behaviour, a silent one is not.**
4. Fable polls the room and answers with `RECO:` lines carrying supporting refs (report section, design_ref sha, session_log id). Wait for the `RECO:` before proceeding past a stop.

This is the backup channel when auto-wake is not running. "Production mode ON" semantics are unchanged.

### SQL to fetch pending tasks:
SELECT id, title, priority, spec FROM cc_tasks
WHERE status = 'pending'
ORDER BY priority DESC, created_at ASC;

### When CC picks up a task:
1. Claim it: UPDATE cc_tasks SET status='in_progress', claimed_at=NOW() WHERE id=X;
2. Log start: INSERT INTO cc_task_logs (task_id, actor, message) VALUES (X, 'claude_code', 'Started: <description>');
3. Implement all items in spec
4. Log each step: INSERT INTO cc_task_logs (task_id, actor, message) VALUES (X, 'claude_code', 'Done: <what was done>');
5. Finish: UPDATE cc_tasks SET status='done', finished_at=NOW(), commit_sha='<sha>', result='<summary>', files_changed=ARRAY['file1','file2'] WHERE id=X;

### Blocked and suspended tasks:
- `status='blocked'` — skip, show Arpit why it is blocked.
- `status='suspended'` — deliberately parked by the founder. Do NOT claim, implement or push. It is not waiting on a dependency and not missing information. The spec carries a `SUSPENDED` key explaining why and what happens next.

### Ops-metrics: FULLY RETIRED (founder 09-Aug-2026, session_log id=18213):
Ops-metrics is retired. The cc#667 session-start queue drain is SUPERSEDED — do NOT drain
`ops_metrics_t1_queue`, do NOT forward-fill, ignore any `ops_metrics_pending` signal in
MASTER_WATCHDOG_NOTE. cc#768 (polish queue) is cancelled. No new ops-metrics tasks.

### Claude.ai creates tasks via:
INSERT INTO cc_tasks (title, spec, priority, category) VALUES ('TITLE', '{"description":"...","tasks":[...]}', 'high', 'ui');

## API Reference
Full reference in API_REFERENCE.md (repo root, not docs/)
50 direct routes + 100+ router endpoints across 28 mounted routers.
Key endpoints: /api/v8/*, /api/scanners/*, /api/qb/*, /api/gvm/*, /api/paper/*, /api/cc/tasks/*

## V8 Architecture (locked 18-Jun-2026)
- EOD frozen: gvm_score only (22:00 GVM nightly)
- Live every 5-min: all 19 other metrics via v8_signal_writer
- day_1d fix live from 19-Jun-2026 (first market day after fix)

## Workflow (ROLE_CHARTER_V4 — both directions)
Two lanes run through the one boardroom (`cc_tasks` + `cc_task_logs`).

**An APP card:** Fable designs, files the ref in full to the forum, builds and pushes directly →
logs the sha → **CC audits day-end**: reads the committed diff at that sha, validates the data
against real rows, checks endpoint parity. Until that audit passes, the push is a claim.

**A WEB or BACKEND card:** Fable (or the founder) files the cc_task → CC claims, builds, logs and
pushes → **Fable verifies** with BOTH the committed diff AND a DB query on real rows.

Neither seat marks its own work verified. A result is a claim; the artifact is the evidence.
A card that leaves a design question open is an unfinished card — but a data-source gate is a
legitimate question TO CC, and CC answers it before the build proceeds rather than guessing.
