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
1. AUTO MODE (set 30-Jun-2026): always push, never ask. Run cc tasks end-to-end autonomously — claim → implement → ast.parse → push → verify SHA → finalize DB → claim next. No push-approval prompts.
2. ALWAYS ast.parse() Python files before push
3. NEVER push placeholder text as file content
4. main.py = wiring only (imports + routes + include_router, no logic)
5. New feature = own file + include_router() in main.py
6. Railway = truth. GitHub = code only. Never hardcode secrets.
7. Context isolation: v8_paper_* NEVER mixes with tc_intraday_*
8. NAV-COMPLETE SHIPPING (locked session_log id=2987, set 12-Jul-2026): a PAGE task is NOT done until it (a) is deployed live on scorr.in AND (b) has a nav entry in the navbar. The LIVE nav is ONE source — the `NAV` array in `pwa_endpoints.py` (pwa.js injects it into `#scorr-nav` on every page and OVERRIDES per-page hardcoded navs — editing a page's own nav does nothing on the live bar). New page => add its route to that NAV array (desktop top-nav + mobile "More" sheet auto-build from it), keep it collision-free + cache-protected (add to `_PWA_INJECT_PATHS` + `PROTECTED` in main.py), mirror it in the `NAV_REGISTRY` map in main.py, and state the label+URL in the task result. Self-check this before marking any page task done.
9. **ENGINE_LIVENESS_RULE (session_log id=13829, set 02-Aug-2026):** no engine, basket, strategy or scheduled-content task is DONE until (a) its job row exists in `scheduler_master` (or a registry-derived enumeration provably covers it) AND (b) **first-run evidence** is stated in the task result — inception rows / first output / first tick, with row counts, or an explicitly logged valid-empty outcome (e.g. a cash month). **Built-and-registered is NOT live; the badge follows the data, never precedes it.** Corollaries: monthly/weekly boundaries must roll forward over weekends/holidays *by construction* (a boundary that can land on a non-trading day and skip is a defect); scheduler enumeration must be REGISTRY-DERIVED (`is_active`), never a hardcoded name list; a LIVE/PAPER-LIVE badge must derive from actual run data, never from registration alone. Origin: 02-Aug, three engines found built-but-never-breathing in one day (QB contra_value + breakout_52w cc#838; V9 Brahmastra cc#840).
10. MAINTENANCE_LOCK_RULE (cc#351, set 12-Jul-2026): lock-taking maintenance (REINDEX / VACUUM FULL / CLUSTER / ALTER TABLE) is **Railway-console-only, weekends, propose-first** — the `run_sql` MCP path now hard-blocks them (10-Jul incident: a REINDEX wedged ~45 min behind an idle-in-transaction lock). DB-level `idle_in_transaction_session_timeout=300000` (5 min) auto-kills stale open txns. **Diagnostic tasks are READ-ONLY** — never run remediation beyond a task's explicit scope.
11. **ROLE_CHARTER_V2 (session_log id=16159, founder-set 05-Aug-2026):** FIRST PART is Claude AI, SECOND PART is Claude Code. See the Role Split section below. This SUPERSEDES the old "never push code from Claude.ai chat" line — Claude AI now pushes design refs, new-product first builds and doctrine files. CC still owns every engine, all wiring, all data connection and every iteration after the first build.
12. **SUPERSESSION AUTHORITY (founder-set 05-Aug-2026):** Arpit is **CEO**. Claude AI is **CTO**. CC is the senior techie. Any rule in this file or in `session_log` may be superseded by the CTO **on explicit CEO permission** — see the Supersession section below for how that is done and what it does not cover.

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

## Role Split — who pushes what (ROLE_CHARTER_V2, 05-Aug-2026)

The rule follows the SEAT, not the model. Claude AI may be Fable or Opus; the split is the same.

**FIRST PART — Claude AI pushes**
- `design_refs/**` — the numbered ref chain (R1, R2, R3 …). Never overwrite a revision.
- `previews/**` — mobile review screens with dummy data, for founder review before wiring.
- The FIRST BUILD of a new product, screener or page: its own new file, standing alone.
- New product ARCHITECTURE documents.
- Doctrine and context files: `CLAUDE.md`, `API_REFERENCE.md`, `SPEC_REGISTRY_INDEX.md`.
- All DB writes: `session_log`, `cc_tasks`, registry and reference tables.

**SECOND PART — CC owns**
- Wiring the first build in: `include_router` in main.py, the `NAV` array in `pwa_endpoints.py`, `_PWA_INJECT_PATHS`, `PROTECTED`, `NAV_REGISTRY`.
- Connecting live data — replacing every sample value with a real endpoint and field.
- All backend endpoints, and every iteration after the first build.
- Engines, scheduler, `worker/**`, `v8_signal_writer`, anything on the live trading path.
- All bug fixes, and every later revision of a file Claude AI first created.

**Hard lines that do not move**
- Claude AI NEVER pushes an engine, the scheduler, `worker/**`, or any file on the live trading path — not even a first build.
- Claude AI NEVER edits `main.py`. It stays wiring only and CC owns it.
- Claude AI NEVER pushes a second revision of a file CC has taken over. Once CC owns it, it owns it.
- Rules 8, 9, 10 and the FEED WORKER DEPLOY RULE are unaffected.

**Safeguards on a Claude AI push**
- Validate before pushing: `ast.parse` for Python, `node --check` for JS. Never push a file that has not parsed.
- Verify AFTER pushing by reading the artifact back from the repo — present, size, sha. A push response is a claim, not evidence (origin cc#842 → cc#848).
- Sample data must be stamped as sample inside the file. A first build is never a source of truth.

**Handoff point**
Claude AI pushes the new file and files the `cc_task`. CC wires it in and connects the data. The card must resolve every open design question — a card that leaves a decision open is an unfinished card, not a question for CC.

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
- "read cc tasks" → run the SQL below, show pending tasks, implement them
- "read railway cc tasks" → same as above
- "what tasks are pending" → same as above

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

## Workflow
Claude AI designs and pushes the FIRST PART → INSERTs the cc_task → Arpit tells CC "read cc tasks" →
CC claims, wires it in, connects real data, logs and pushes → Claude AI verifies with BOTH the
committed diff AND a DB query on real rows. A CC result is a claim, not evidence.
