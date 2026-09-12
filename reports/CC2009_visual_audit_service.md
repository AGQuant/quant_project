# cc#2009 — Visual audit service enablement, and a bigger finding underneath it

## Item 1-2: the artifact IS on the branch. The real problem is one level up.

`github_read` on `main` for `visual_audit.py` correctly returns not-found — **that file has never
existed on `main`.** It exists, fully, byte-for-byte as pushed, on
`refs/heads/claude/cc-framework-production-setup-ac78qx` — confirmed just now by calling GitHub's
own API directly (not local git) against that exact ref:

- `visual_audit.py` — present, sha `dbebf4a9`, at repo root, landed in commit `4c92d09` (cc#2007)
  and unchanged through the current branch HEAD `458daa4` (cc#2008).
- `visual_audit_endpoints.py` — present, sha `49bf6f21`, same location, same history.
- `main.py`'s `include_router(visual_audit_router)` — part of the same `4c92d09` commit, on the
  same branch.
- The two table migrations (`visual_audit_captures`, `visual_audit_results`) — these went straight
  to the real Railway Postgres via direct SQL, not through git at all, so they are live regardless
  of branch/PR state. Confirmed still present, 0 rows each (per this card's own `finding` field).

**So `4c92d09` was not a partial push — nothing from it is missing or malformed.** cc#2009's own
`finding` field ("module is absent from repo root, worker/, tools/ and lib/") describes what a
`github_read` against `main` sees, correctly, because **nothing from this entire session is on
`main`.**

## The bigger finding: zero pull requests, ever, for this branch — this is not a cc#2007-specific problem

Checked directly: `list_pull_requests(head="AGQuant:claude/cc-framework-production-setup-ac78qx",
state="all")` returns an **empty array**. No PR — open, closed, or merged — has ever existed for
this branch. Every card this session pushed (cc#1978 through cc#2008: the marker-tick writers,
v8.html's swipe deck, check.html's rule descriptions, the chain-grid backend and frontend, the
Home Derivatives removal and chain popup, the Market Mood capsule, the visual-audit harness, and
this card's own Dockerfile/service-config) sits on this one unmerged branch. If Railway's auto-
deploy watches `main` — which `list_pull_requests` returning nothing, and `github_read(main)` 404ing
on files that plainly exist on the branch, both point to — **none of it has reached the live app**,
regardless of how many `git fetch`/`rev-parse`/`merge-base --is-ancestor` checks confirmed the
branch itself was pushed correctly. Those checks (which this session ran faithfully after every
push) prove the branch is intact on origin; they do not prove the branch is deployed. This is a
different, narrower claim than the session had been making, and the gap between them is exactly
what this finding closes.

**Not resolved unilaterally here.** The task-system instructions this session runs under are
explicit: *"Do NOT create a pull request unless the user explicitly asks for one."* Opening a PR to
close this gap is outward-facing and exactly the kind of action that instruction gates — so it is
not done in this push. Logged as a QUESTION in `cc_task_logs` for the founder/Fable: **should a PR
from this branch to `main` be opened now (by whoever is authorized to ask for it), or does
production actually deploy some other way this session has not been shown?** Every card's own
`result` text this session already said "Card not done — Fable verifies" for exactly this class of
reason; this finding is the concrete mechanism behind why that caution was warranted.

## Items 3-6: the service can now be provisioned as soon as that question is answered

**New files, this push (git only — nothing deployed by these alone):**
- `Dockerfile.visualaudit` — `FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy` (ships
  Chromium + every native library Playwright needs, already version-matched — the reason a
  Dockerfile is used here instead of extending Nixpacks' `aptPkgs`), copies the repo (the crawler
  reads `pwa_endpoints.py`'s NAV array as text at runtime, so it needs the real checkout beside it,
  not just its own file), installs the one extra dependency (`psycopg[binary]`) the base image
  doesn't already carry, `CMD ["python3", "visual_audit.py"]`.
- `railway.visualaudit.json` — modelled on `railway.worker.json`'s own shape: `watchPatterns`
  scoped to exactly the 3 files this service owns (`visual_audit.py`, `Dockerfile.visualaudit`,
  `railway.visualaudit.json` itself) — nothing else in the repo redeploys this service, matching the
  worker's own "scoped watch, not a global one" convention. `restartPolicyType: ON_FAILURE`,
  `restartPolicyMaxRetries: 2` — lower than the worker's 10 (a missed audit day is not a live-feed
  gap; 2 retries covers a transient blip without masking a real, persistent failure for a week).

**Confirmed untouched:** `nixpacks.toml` is byte-identical to before this card (diffed against the
committed version to confirm). Nothing under `worker/**` was touched.

### Console checklist for the founder (or Fable) — everything this needs, so no follow-up question is required

1. **Create a new Railway service** in the same project as the web service and the `truthful-friendship` feed worker.
2. **Source**: same GitHub repo, same branch this session has been pushing to — **once the branch/PR
   question above is resolved**, since Railway needs to build from wherever the code actually lives.
3. **Builder**: Docker (not Nixpacks). **Dockerfile path**: `Dockerfile.visualaudit`.
4. **Config-as-code path**: `railway.visualaudit.json`.
5. **Service type**: Railway's **Cron Job** type, not a long-running service — `visual_audit.py`'s
   own `main()` runs one crawl and exits (confirmed by reading it: no loop), matching a cron job's
   run-once-per-firing model exactly, and avoiding paying for an always-on idle container.
   **Schedule**: daily, outside market hours — the `scheduler_master` row already created for this
   job (`visual_audit_crawl`) states `cron_expr='0 3 * * *'` (03:00 IST) as the intended cadence;
   set the Railway cron schedule to match so the registry row and the actual trigger agree.
6. **Start command**: leave blank — the Dockerfile's own `CMD` handles it. If Railway's console
   requires an explicit value anyway, use `python3 visual_audit.py`.
7. **Environment variables this service needs** (names only, no values — Railway is truth, GitHub
   is code only):
   - `DATABASE_URL` — same value the web service already has, copied onto this new service.
   - `SCORR_AUTH_PASSWORD` — same value the web service already has, copied onto this new service.
     This is the ONE credential the crawler needs to log in; it is the site's single shared
     password, not a separate account.
   - `VISUAL_AUDIT_BASE_URL` — **new**, set to the production URL (`https://scorr.in` or
     whichever is the live public domain — API_REFERENCE.md's own header names
     `https://quantproject-production.up.railway.app` as the Base URL; confirm which one the founder
     wants crawled and set that one). Without this the crawler defaults to `http://127.0.0.1:8000`,
     which is not reachable from a separate service.
   - `CC_CHROMIUM` — optional; leave unset. The base image's own Chromium is on its default PATH
     entry, and `visual_audit.py`'s own default (`/opt/pw-browsers/...`) is this CC session's
     sandbox path, not the Docker image's — Playwright's `sync_api` resolves its own bundled browser
     automatically when no override is given, so leaving this unset is correct here, not an
     oversight.
   - `VISUAL_AUDIT_SHOTS_DIR` — optional; leave unset (defaults to `visual_audit_shots/` inside the
     container, which is fine — images are transient evidence for a failing check, not something
     that needs to survive a redeploy per the founder's own storage revision).

### Item 6 — manual trigger, on demand

Once the service exists, Railway's own **"Trigger"/"Run now"** action on a Cron Job service (in the
Railway dashboard, on the service's Deployments tab) fires `python3 visual_audit.py` immediately,
using whatever environment variables are already set — no separate command needed. If the founder
or Fable would rather run it from a shell attached to the service, the equivalent command is:
`python3 visual_audit.py` (reads `VISUAL_AUDIT_BASE_URL`/`SCORR_AUTH_PASSWORD`/`DATABASE_URL` from
the environment already set on the service; no flags required).

## What this card did NOT do

Did not open a pull request (gated by explicit instruction; logged as a question instead). Did not
modify `nixpacks.toml`, anything under `worker/**`, the five checks or their thresholds, or the
`visual_audit_results`/`visual_audit_captures` schema. Did not run the crawl — first run still
depends on the founder's console work (service creation) AND the branch/PR question above, whichever
is resolved second gates whichever is resolved first.
