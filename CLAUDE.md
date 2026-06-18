# CLAUDE.md — Scorr Platform Context

## What is Scorr
AI-native investment research platform. scorr.in
Mission: democratise institutional-grade investing for retail India.
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
| scorr_cockpit.html | Main nav shell — nav links to all pages | ~41KB |
| scorr_scanners.html | Scanners page (3 tabs) | ~8KB |
| scorr_home.html | Home page | ~6KB |
| docs/API_REFERENCE.md | Full endpoint reference (created by CC) | |

## CC Task System
Claude.ai creates tasks in Railway session_log with category='cc_task'.
To get your task list run this SQL via Scorr MCP:
  SELECT title, details FROM session_log WHERE category='cc_task' AND details::text LIKE '%pending%' ORDER BY session_ts DESC;

Current pending tasks: CC_TASK_001_UI_FIXES (scorr_home.html nav, logout, Scorr branding, back tabs)

## V8 Architecture (locked 18-Jun-2026)
- EOD frozen: gvm_score only (22:00 GVM nightly)
- Live every 5-min: all 19 other metrics via v8_signal_writer
- COALESCE protection: day_1d, mom_2d, sector_week, sector_month
- day_1d fix live from 19-Jun-2026 (first market day after fix)

## API Reference
Full reference in docs/API_REFERENCE.md
Railway summary in session_log title='API_REFERENCE_18JUN2026'
50 direct routes + 100+ router endpoints across 28 mounted routers.

## Workflow
Claude.ai (design + spec) → Railway cc_task → Claude Code (implement + push) → Railway (deploy) → Claude.ai MCP (verify)
Never push code from Claude.ai chat. Claude Code owns all file changes.
