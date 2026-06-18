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
1. ALWAYS ask before git push — no silent commits
2. ALWAYS ast.parse() Python files before push
3. NEVER push placeholder text as file content
4. main.py = wiring only (imports + routes + include_router, no logic)
5. New feature = own file + include_router() in main.py
6. Railway = truth. GitHub = code only. Never hardcode secrets.
7. Context isolation: v8_paper_* NEVER mixes with tc_intraday_*

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
