# cc#2007 — Visual audit harness: capture, five checks, failures-only reporting

Founder direction 12-Sep-2026 ~08:30 IST (scope) and ~08:40 IST (storage revision). Problem stated:
the founder is currently the only detector of visual defects — black popup on white theme, low
contrast, clumsy layout — across every page and theme. This builds the machine that finds them.

## What this ships

- **`visual_audit.py`** (repo root, deliberately NOT under `worker/` — do_not_touch names that tree
  explicitly, and a file there would bounce the unrelated fyers feed service on every future edit
  to this one, per FEED WORKER DEPLOY RULE cc#416). The crawler: route enumeration, login, capture,
  all five checks, DB writes, conditional image storage, 7-day image purge. Runnable standalone:
  `python3 visual_audit.py --base-url https://scorr.in`.
- **`visual_audit_endpoints.py`** — `GET /api/visual-audit/failures`, the failures-only read
  endpoint (item 12), `include_router`'d in `main.py`. This is the ONE piece of this card that runs
  in the web process — a plain DB read, not the crawler itself.
- Two new tables: `visual_audit_captures` (one row per route×theme×viewport attempt — status, hash,
  conditional image path) and `visual_audit_results` (one row per check per element, PASS and FAIL
  alike, per item 11).
- A `scheduler_master` row (`visual_audit_crawl`, `active=true`, `category='external_job'` —
  deliberately NOT `'scheduler_loop'`, see below).

## Item 3 — route enumeration, registry-derived

`parse_nav_routes()` reads `pwa_endpoints.py` fresh off disk every run and depth-scans the literal
`var NAV = [...]` block (not a bounded regex — that array runs hundreds of lines with `//` comments
that themselves contain brackets, which would break a naive "first `];`" match). Run against the
REAL, current file today: **42 routes, zero duplicates, every path starts with `/`** — output
included in the verify section below. A NAV edit is picked up on the crawler's next run with no
change needed here, which is what "never a hardcoded page list" means in practice.

## Item 2 — "service login"

This site has ONE shared password (`SCORR_AUTH_PASSWORD`, `scorr_auth.py`), not per-user accounts
— there is no separate service account to provision. `login()` drives the real `/login` form as a
browser would: fills `input[type=password]` (selected by TYPE, not by its randomized per-load NAME
— sidesteps the anti-autofill randomization entirely rather than reverse-engineering it), clicks
`button[type=submit]`, confirms the resulting `scorr_auth` cookie. If that cookie never appears, the
crawl stops and reports rather than falling back to whatever public pages happen to be reachable —
the card's own gate line, implemented literally in `run_crawl()`.

## Item 4 — themes and viewports

Two representative sets rather than all ten the switcher offers: `goldnight` (dark, the app's own
`DEFAULT`) and `aquawhite` (light) — both confirmed **COMPLETE** in the `theme_validate` token-set
report (zero missing keys each), so a captured defect reads as a real page issue, not an artifact of
an incomplete theme. Forced via the app's OWN already-built `?theme=` one-load preview query param
(`pwa_endpoints.py`'s `APP_THEME_RESOLVE_JS`) — deterministic, and it is the app's own mechanism,
not a new one invented for this card. Viewports: mobile 412×915 (matches `tools/render_check.py`'s
own Android reference width, rather than a third convention) and desktop 1440×900.

## Items 6–10 — the five checks, and item 7's "reuse their token registry" specifically

`theme_validator.py` (the module `mcp__Scorr__theme_validate` wraps) is a STATIC-SOURCE scanner —
it parses stylesheet text for `var(--x, #literal)` and bare literals, and never renders a page. That
is exactly why cc#1941/cc#1970's bug (a JS-appended overlay landing outside every CSS scope, so its
own correct-looking `var()` names resolved to nothing) was invisible to it for months — read
directly while building this card, not assumed. That check already runs as its own push gate;
re-implementing literal-colour scanning here would be the "second registry" the card says not to
build. What a RENDERED page uniquely exposes, and what a static scanner structurally cannot, is the
founder's own example — a live element whose computed background is inverted against the page's own
active theme. That is what the `theme_leak` check measures (luminance of an element's own painted
background vs. the page's overall luminance). It does not re-flag source literals; that stays
`theme_validator`'s job, on purpose.

- **contrast**: WCAG relative-luminance ratio, walking up the tree for the first non-transparent
  background (an element's own `background-color` is very often `transparent`). 4.5:1 body text,
  3:1 for ≥24px or ≥18.66px-bold ("large text" per the WCAG definition).
- **theme_leak**: element background luminance inverted against `document.body`'s own, on elements
  ≥80×40px (skips icon-sized chips so this stays a founder-visible-surface check, not a pixel hunt).
- **overflow_clipping**: document-level horizontal scroll, a container clipping content with no way
  to scroll to it, and an element extending past the viewport's right edge — adapted from
  `tools/render_check.py`'s (cc#1133) own proven `hiddenByAncestor`/`vis` logic, credited inline
  rather than re-derived.
- **tap_target**: every visible `button`/`a[href]`/`[onclick]`/`[role=button]`/form control under
  44×44 CSS px.
- **empty_broken**: a known error-string scan (`undefined`, `NaN`, `[object Object]`, `Could not
  load`, stack-trace fragments) across the whole page, plus a heuristic pass over this codebase's own
  content-container class families (`.card/.c/.sect/.oib/.vpage/.oic/.deriv-row/.apf-note`) for one
  that occupies real space but paints zero characters of text. Stated as a heuristic, not exhaustive
  — a genuinely empty STATE already renders its own "nothing here" sentence, which is why this only
  fires on truly empty, not on an honest empty-state message.

## Verify — what is actually confirmed, and what is not (read the gate line before trusting "done")

**Confirmed, against the real code, this session:**
- `parse_nav_routes('pwa_endpoints.py')` run for real: **42 routes, 0 duplicates**, every entry's
  `path` starts with `/`; spot-checked the first 6 and last 4 against the file's own comments
  (e.g. `/alerts` carries the `'d'` flag exactly as cc#1536/cc#1585's own comments describe).
- `run_checks()` — the literal shipped function, not a reimplementation — run via the real
  `playwright.sync_api` against a synthetic page built with one deliberate defect of each of the
  five kinds plus a "good" sibling for each. All eleven expectations passed: low-contrast text FAILs
  (1.36:1) / good text PASSes (18.88:1); a dark box on the light theme FAILs `theme_leak` / a normal
  light card does not false-positive; a clipped element, a content-overflowing container and the
  document itself all correctly FAIL `overflow_clipping`; a 20×20 button FAILs `tap_target` / a
  48×48 button PASSes; an empty card FAILs `empty_broken`, a filled card does not, and page text
  containing "undefined" is caught as a visible error string. Full output kept alongside this report
  for anyone who wants the raw run rather than the summary.
- One minor known double-count found in that same run, not a defect: a container with
  `overflow:hidden` clipping an oversized child gets its own `overflow_clipping` FAIL row IN
  ADDITION TO the clipped child's row — two rows for one visual defect. Left as-is (both statements
  are independently true and the spec does not ask for de-duplication); noted here rather than
  silently smoothed over.
- `node --check` / `ast.parse` clean on `visual_audit.py`, `visual_audit_endpoints.py`, `main.py`.

**NOT confirmed — and why, per the card's own gate ("if the service login cannot reach PROTECTED
routes, STOP and report rather than capturing only public pages and presenting that as full
coverage"):**
- `login()` against the real site, the DB write path in `run_crawl()`, and the `/api/visual-audit/
  failures` endpoint are none of them exercised against a live database or a live deployed instance.
  This CC container has no route to scorr.in (the same limitation `tools/render_check.py`'s own
  docstring states for itself) and no `DATABASE_URL` reachable from a plain Python process here
  (confirmed by trying: `psycopg` is not even installed in this sandbox, and `mcp__Scorr__run_sql`'s
  own connection is not exposed to a script running outside that tool). The SQL itself is modelled
  directly on `deriv_metrics._conn()`'s own established convention and its column counts were
  checked by hand against the tables just created, but "checked by hand" is a claim, not the
  artifact — a real run against a real database is what would make it evidence.
- **First-run evidence (rule 9) does not exist yet, and this states that plainly rather than
  fabricating it.** Two things are needed to produce it, neither of which a `git push` accomplishes
  on its own — the same shape as the fyers feed worker's own documented one-time setup:
  1. A new Railway service (this job must not run inside the web process, per do_not_touch — the
     same reasoning that already gives the feed worker its own service, `truthful-friendship`),
     pointed at `python visual_audit.py`, on its own cron schedule.
  2. `SCORR_AUTH_PASSWORD` copied onto that new service's environment (it is not the web service's
     that this job would inherit automatically).
  The `scheduler_master` row is in regardless (`active=true`, `category='external_job'` — chosen
  deliberately DIFFERENT from `'scheduler_loop'` so `scheduler.py`'s existing in-process dispatcher,
  which only reads `category='scheduler_loop'` rows, never tries to run a headless-Chromium crawl on
  its own event loop) — registered, not live, and its own `notes` column says so. `last_run_at` is
  NULL. Built-and-registered is not live; this is the valid-empty-outcome case rule 9 itself allows
  for, stated with the two concrete steps that close it rather than left as a vague "pending."

## What this does NOT include

Fixing any defect a run turns up (out_of_scope: "Fixes are separate cards raised from the audit
output"). Performance auditing (explicitly sequenced after this, per the card). Aesthetic judgement
— every check here measures a number against a threshold; none of them opine on whether a layout
looks good. Card not done — Fable verifies, and the one Railway-console step above is the founder's
or Fable's to take before a first live run is possible.
