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
| v8_signal_writer.py | Live 5-min signal engine (v2.4.0) | ~61KB |
| v8_engine.py | EOD engine runs 15:45 IST | ~23KB |
| scanner_endpoints.py | Scanner API + /scanners HTML route | ~6KB |
| scorr_cockpit.html | Main nav shell (41KB) — nav links to all pages |
| scorr_scanners.html | Scanners page (3 tabs) |
| tc_intraday.py | Intraday paper engine — INACTIVE from scheduler |

## Pending Tasks (do these first)
1. Wire scanner_endpoints into main.py:
   - Add: `from scanner_endpoints import router as scanner_router`
   - Add: `@app.get("/scanners")` route → scorr_scanners.html
   - Add: `app.include_router(scanner_router)`

2. Fix scorr_cockpit.html nav (line 218):
   - FROM: `<a class="mnav-item" href="/intraday"><span>⏱</span>Intraday</a>`
   - TO:   `<a class="mnav-item" href="/scanners"><span>⊞</span>Scanners</a>`

3. Note: scorr_cockpit.html is currently 20 bytes (placeholder) — restore from git history first

## V8 Architecture (locked 18-Jun-2026)
- EOD frozen: gvm_score only (22:00 GVM nightly)
- Live every 5-min: all 19 other metrics via v8_signal_writer
- COALESCE protection: day_1d, mom_2d, sector_week, sector_month
- Sector aggregates: _update_sector_aggregates_sql() — single SQL pass

## Workflow
This is a Claude Code repo. Changes flow:
  Claude.ai (design + spec) → GitHub Issue [CC] → Claude Code (implement + push) → Railway (deploy) → Claude.ai MCP (verify)

Never push code from Claude.ai chat. Always use Claude Code for file changes.
