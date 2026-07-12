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
9. MAINTENANCE_LOCK_RULE (cc#351, set 12-Jul-2026): lock-taking maintenance (REINDEX / VACUUM FULL / CLUSTER / ALTER TABLE) is **Railway-console-only, weekends, propose-first** — the `run_sql` MCP path now hard-blocks them (10-Jul incident: a REINDEX wedged ~45 min behind an idle-in-transaction lock). DB-level `idle_in_transaction_session_timeout=300000` (5 min) auto-kills stale open txns. **Diagnostic tasks are READ-ONLY** — never run remediation beyond a task's explicit scope.

## Deploy policy
- RULE_7 (deploy-window "no deploy 09:00–15:35 IST", referenced in cc_task specs) is **SUSPENDED as of 07-Jul-2026** — dev-stage, product NOT live (policy id=1713). Deploy anytime, including market hours; task specs that reassert RULE_7 are overridden while in dev mode. Re-instate this window only when the product goes live.
- **FEED WORKER DEPLOY RULE (set 09-Jul-2026, cc#339):** the fyers feed worker (Railway service `truthful-friendship`) redeploys on changes to `fyers_feed.py` / `fyers_autologin.py` / `fyers_backfill.py` / `nse_holidays.py` (watch-paths in `railway.worker.json`). RULE_7's dev-stage suspension does **NOT** apply to the worker: changes to those files deploy the worker **deliberately and OUTSIDE market hours (after 15:30 IST)** unless Arpit explicitly approves a market-hours worker deploy — a mid-market reboot is a coin-flip on re-auth (root cause of the 07-Jul + 09-Jul 100-min feed freezes). App/UI/task pushes are unaffected (worker no longer bounces on them). One-time setup: point the `truthful-friendship` service's Railway config-file path at `railway.worker.json`.

## Reporting style (CC → Arpit)
- After a push/deploy: keep the reply SHORT — confirm what was pushed (file + commit/sha) and state what's next (next pending task or remaining items). No long recaps or re-explanations.
- **PUSH CONFIRMATION SIGNAL (set 08-Jul-2026):** every time code is pushed to `main`, end the reply with the line `DONE WHAT NEXT` in ALL CAPS (on its own line, after the short file+sha confirm). This is Arpit's deploy-confirmed handshake — seeing `DONE WHAT NEXT` = task is pushed/deploying and CC is ready for the next. Canonical: session_log DONE_WHAT_NEXT_PUSH_SIGNAL_V1.

## Key Files
| File | Purpose | Size |
|---|---|---|
| main.py | FastAPI app, all routes + routers | ~60KB |
| scheduler.py | Background jobs. start_background(app, base_url, token) | ~14KB |
| v8_signal_writer.py | Live 5-min signal engine | ~61KB |
| v8_engine.py | EOD engine runs 15:45 IST | ~23KB |
| scanner_endpoints.py | Scanner API + /scanners HTML route | ~6KB |
| cc_task_endpoints.py | Task queue API (to be created) | |
| scorr_cockpit.html | Main nav shell | ~41KB |
| scorr_scanners.html | Scanners page (3 tabs) | ~8KB |
| scorr_home.html | Home page | ~6KB |
| API_REFERENCE.md | Full endpoint reference (repo root) | ~14KB |

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

### Blocked tasks:
If status='blocked' — skip, show Arpit why it is blocked.

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
Claude.ai → INSERT cc_tasks → Arpit tells CC "read cc tasks" → CC claims + implements + logs → Claude.ai verifies
Never push code from Claude.ai chat. Claude Code owns all file changes.
