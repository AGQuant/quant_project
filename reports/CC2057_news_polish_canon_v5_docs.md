# cc#2057 — CLAUDE.md + SPEC_REGISTRY_INDEX.md: propagate NEWS_POLISH_CANON_V5

Founder ask: the news-polish batch was raised to 12 (session_log 45416) and he asked that it be
updated everywhere so it can never drift. Two repo docs CC reads on every wake still carried the
old figure. Docs-only — no code, no `polished_news` writes (cc#1837 stands).

This is propagation of a supersession Fable already made through the correct channel (session_log
45416 written today, 33626/40976/45415 already moved to `archived_superseded`), not a new
supersession decision of my own.

## 1. CLAUDE.md

Retitled the section `## News polish (NEWS_POLISH_CANON_V5, session_log 45416)`; the batch-shape
line now reads 12 items = 2 AI Editorial + 5 Domestic + 2 Global + 1 IPO + 2 Stock Views, with one
new sentence stating Stock Views is now part of every batch, citing STOCK_VIEWS_FRAMEWORK_V2
(session_log 13456) as the card asked. Rest of the section — CC never writes `polished_news`,
feeders stay alive, stale-news gaps aren't incidents — untouched, verbatim.

## 2. SPEC_REGISTRY_INDEX.md

Regenerated via `tools/gen_spec_index.py`. This sandbox has no `DATABASE_URL` (DB access here is
MCP-mediated, not a direct Postgres connection), so the generator's own offline path was used: the
same `DOCTRINE`-category query the script runs internally, pulled via `run_sql` (754 rows) and fed
through `--rows`. `45416` now appears live under `spec_locked`; `33626`/`40976`/`45415` are absent
from every doctrine table (confirmed already `archived_superseded` in the DB before this card ran).
A new row was added by hand to the script's own hardcoded `TRAIL` table (it isn't DB-generated) per
the card's exact wording: `40976 (+33626, 45415) -> 45416` on composition, with `40972` (execution
mode) stated as still live and untouched.

**The regeneration also picked up 72 days of otherwise-unrelated drift** (682 → 754 doctrine
entries; the committed file was last generated 02-Sep-2026) — this is the generator's own stated
purpose ("so the index can be current whenever anyone wants it to be"), directly what the card's
own scope item 2 asks for ("re-run tools/gen_spec_index.py"), and every added/removed row traces to
a real, already-authoritative `session_log` row — not something invented for this card. Confirmed
`40972`'s absence from the doctrine tables is pre-existing and unrelated to this change: its live
DB category is `engine`, not one of the indexed doctrine categories, so it was never going to
appear there regardless of the news-polish edit — its "still live" status is exactly what the new
TRAIL row states.

## 3. Repo-wide grep for other stale restatements (scope item 3)

Searched `docs/`, `reports/`, `*.md`, `CLAUDE.md` for `"1 AI Editorial + 9"`, `"9 Shorts"`,
`"10 items = 1 AI"`, `"10 items"`, `"6 Domestic / 2 Global / 1 IPO"`, and the old split without
Stock Views. **Clean — nothing else to fix:**
- `tools/gen_spec_index.py`'s own new TRAIL row names the old canon (V3, its retired ids) —
  correct: every row in that table names the *retired* thing by design, exactly the "dated log,
  leave it alone" case the card itself distinguishes.
- `mobile_endpoints.py:1465`'s "10 items inside a fixed-height scroll box" is an unrelated UI
  comment about a scrollable element, not a news-polish batch size.
- The one hit inside `CLAUDE.md` itself is a substring match against the *already-corrected* new
  V5 line (the first four quantities are numerically unchanged from V3 to V5 — only Stock Views was
  added — so the substring search coincidentally matches the correct text too).

No other file states the old batch shape as current guidance.

## Verify

`github_read` (post-push) confirms: `CLAUDE.md`'s section names 45416 and states 2/5/1/2/2 = 12;
`SPEC_REGISTRY_INDEX.md`'s section 8 (`spec_locked`) carries 45416, `33626`/`40976`/`45415` are
absent from every live doctrine table, and the trail row is present and correctly worded. This grep
report was posted to `cc_task_logs` per the card's own verify requirement.
