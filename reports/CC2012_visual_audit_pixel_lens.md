# cc#2012 — Visual audit PIXEL LENS: captures in Postgres, token-gated serving, a persistent worker

Founder ruling 12-Sep-2026 ~13:30 IST: Fable takes the full design and audit view of the app and
builds whatever it takes. Supersedes the SERVICE TYPE and IMAGE STORAGE sections of
`reports/CC2009_visual_audit_service.md` and the 12-Sep "images only on FAIL" storage revision.
Built in the Fable seat (founder: "2012 fable territory").

## What was wrong (the card's own `why`, confirmed by reading the code)

cc#2007 wrote PNGs to the crawl container's own disk and returned `image_path` as a string. The
web service is a different container; a cron container is disposed after each run. The images were
write-only — measurements Fable could query, no picture anyone could open.

## What this push lands

**`visual_audit.py` — now a persistent worker, not a one-shot script** (item 4)
- `worker()`: one browser, one login, one DB connection for the life of the process. Every 60s:
  `process_requests()` drains `visual_audit_requests` (status `pending` → `running` → `done` with
  `capture_id`, or `error` with the message; oldest first; one bad request errors ITS row and the
  queue continues), then `daily_crawl_due()` decides whether to run the full crawl.
- `daily_crawl_due()`: 03:00 IST, decided **in-process with a real `Asia/Kolkata` clock** — not
  Railway cron, which is UTC-only (`0 3 * * *` there is 08:30 IST, mid-session). Due when it is at or
  past 03:00 IST today and `scheduler_master.last_run_at` is not already from today in IST. A
  restart at 14:00 on a day the 03:00 run was missed runs it once (catch-up), then waits for
  tomorrow — never double-runs. Tested at the boundaries (below).
- `execute_actions()` (item 5): `{"click": "<selector>"}` and `{"wait_ms": N}` only, max 10 steps,
  a single wait capped at 10s, unknown step shapes skipped — there is no path that evaluates JS.
  Tested: a dark popup that only exists after a click shows up in the `theme_leak` check AND in the
  image; a selector that never appears errors that one capture, not the process.
- `capture_one()`: JPEG quality 80, captured as BYTES (`page.screenshot()` with no `path=`), for
  EVERY capture pass or fail. No shots directory exists any more.
- `_write_capture()` — ONE writer shared by the crawl and the request path — then
  `_apply_retention()` (item 1): the row just written keeps its bytes indefinitely (excluded from
  the UPDATE); every OLDER row of the same (route, theme, viewport) keeps bytes only if it recorded
  a check FAIL and is within 7 days; everything else has bytes nulled. Rows are never deleted —
  measurements, hash and status stay queryable. Tested on a scratch table (below).
- `--once` flag: one iteration then exit — the manual-trigger/smoke-test path.
- Exits only on a fatal error (login impossible, browser gone); a DB hiccup reconnects, a failed
  iteration logs and continues. Dockerfile `CMD` unchanged.

**`visual_audit_endpoints.py`** (items 2/3) — two new routes on the existing router (no
`main.py` change; the router is already included):
- `GET /api/visual-audit/capture/{id}?token=…` → the stored bytes with the stored mime,
  `Cache-Control: no-store`.
- `GET /api/visual-audit/runs/latest?token=…` → `run_id`, `captured_at`, per capture
  `{id, route, theme, viewport, status, fail_count, content_hash, has_image}` (on-demand `req-…`
  captures are not "runs"; this reads the latest full crawl).
- `_token_ok()`: `hmac.compare_digest` against `VISUAL_AUDIT_VIEW_TOKEN`; unset env, empty token or
  mismatch → False → **404** (not 401). Fail-closed. Tested (below).
- **Site-auth exclusion, checked not assumed** (the card's own gate): `main.py`'s `auth_gate`
  redirects to `/login` only for paths in the exact-match `PROTECTED` set or under `/preview/`.
  These routes are not in `PROTECTED`, so they are outside the site-password gate by construction;
  the middleware needed **no change**. Its post-response HTML injection keys on `text/html`, and
  these return `image/jpeg` / `application/json`. `/api/visual-audit/failures` untouched.

**`scheduler_master.py`** (item 7) — `visual_audit_crawl` added to `_WORKER_JOBS`. **What the cc#759
sweep actually keys on:** `run_drift_audit()` retires any active row whose `job_name` is not in
`all_known_jobs()` = scheduler-loop AST enumeration + `_STARTUP_JOBS` + `_WORKER_JOBS` +
`_CHAINED_JOBS`. It does not check file presence at all. The row was `category='external_job'`,
matched none of those, and was retired on 12-Sep — and would have been re-retired on every audit
regardless of what was on `main`. Listing it in `_WORKER_JOBS` (the same hand-entered list
`worker/fyers_feed.py`'s own in-process loops use, for the identical reason) is the fix; flipping
`active` alone would be undone by the next audit (cc#1095's own lesson, recorded two entries above).

**`scheduler_master` row** (item 6): `active=true`, RETIRED marker removed, `module=visual_audit.py`,
`function=worker`, `service=visual-audit`, `category=worker_watchdog`, notes rewritten to describe
the in-process 03:00 IST schedule and the poller, `cron_expr` kept as human-readable intent with
the note stating Railway cron is NOT the trigger. Verified by SELECT after the UPDATE.

**`railway.visualaudit.json`**: `restartPolicyMaxRetries` 2 → 10. cc#2009 sized 2 for a cron job;
a persistent worker that is meant to be restarted needs the feed worker's own ON_FAILURE/10.

**`visual_audit_requests`** table created (id, route, theme, viewport, actions JSONB, requested_by,
requested_at, updated_at, status, capture_id → captures, error; partial index on pending).

## The one blocked step — stated, not routed around

`ALTER TABLE visual_audit_captures` is hard-blocked on the `run_sql` path by MAINTENANCE_LOCK_RULE
(cc#351, rule 10: Railway-console-only, weekends, propose-first). Proposed DDL, logged in the room:

```sql
ALTER TABLE visual_audit_captures
  ADD COLUMN IF NOT EXISTS image_bytes BYTEA,
  ADD COLUMN IF NOT EXISTS image_mime TEXT,
  ADD COLUMN IF NOT EXISTS has_check_fail BOOLEAN NOT NULL DEFAULT false;
```

Three columns, not two: `has_check_fail` is set at insert and makes retention one index-friendly
UPDATE instead of a join per capture. The code targets these columns on `visual_audit_captures`
exactly as the card specifies (deliberately not a side table, so the card's `information_schema`
verify line holds once this lands). Until it runs, the worker's INSERT fails on the missing columns.

## Verify — done here, against the real shipped functions

- `ast.parse` clean: `visual_audit.py`, `visual_audit_endpoints.py`, `scheduler_master.py`. JSON valid.
- Playwright + the real `capture_one`/`execute_actions`/`daily_crawl_due`/`_token_ok`, 18/18:
  JPEG bytes always (FFD8 magic, ~7KB for the synthetic page); `theme_leak` PASS with the popup
  closed and FAIL on `div#popup` after the click action; image differs before/after; 15 steps
  bounded to 10; `{"evaluate":…}`/`{"js":…}`/a bare string skipped; a missing selector → capture
  `status=error`, process alive; IST boundaries 02:30/03:05/14:00 with never-run/yesterday/today
  stamps behave as specified including catch-up and no-double-run; token: env unset → False even
  with a token, empty → False, wrong → False, right → True.
- Retention SQL, the exact statement, on a scratch table in the real Postgres (created, exercised,
  dropped): 6 rows across 3 combos — nulled exactly the 10-day-old FAIL and the 2-day-old PASS;
  kept the latest, the 3-day-old FAIL, and both other combos untouched.

## Verify — NOT done here, and why

The worker LOOP running for real, a capture of the actual deployed app, the served image through
the two routes: this container has no route to scorr.in and no `DATABASE_URL`, and the columns are
not yet on the table. Built-and-registered is not live (rule 9). `last_run_at` is NULL and stays
NULL until the worker's own first full crawl writes it.

## Item 8 — console settings for the founder (PLAIN service, not a Cron Job)

**Crawler service** (new, Docker builder): Dockerfile path `Dockerfile.visualaudit`; config-as-code
`railway.visualaudit.json`; no start command (the Dockerfile `CMD` is `python3 visual_audit.py`);
NOT a Cron Job — a plain always-on service. Env: `DATABASE_URL` (reference the web service's),
`SCORR_AUTH_PASSWORD` (same value as the web service), `VISUAL_AUDIT_BASE_URL=https://scorr.in`.
Optional: `VISUAL_AUDIT_POLL_SECONDS` (default 60). Leave `CC_CHROMIUM` unset (the image's own
Playwright browser is used).

**Web service** (existing): add `VISUAL_AUDIT_VIEW_TOKEN` — ≥32 random bytes, hex, generated by
the founder, never pasted anywhere in the repo or a log.

**Before the first run:** the ALTER TABLE above, in the Railway console.

**Acceptance test Fable will run** (the card's own): `INSERT INTO visual_audit_requests (route,
theme, viewport, requested_by) VALUES ('/m/home','aquawhite','mobile','fable');` → within ~3
minutes `status='done'` with a `capture_id` → `GET /api/visual-audit/capture/{id}?token=…` returns
200 `image/jpeg`; wrong token → 404; no token → 404.

## Addendum — app_route_map (Fable, cc_task_logs 6427; second push)

Fable created `public.app_route_map` (42 rows, one per NAV route: label, surface, nav_flag,
route_group, serving_file, handler, template, nav_position; PK on route). Two asks, both landed:

- **(a) `GET /api/visual-audit/runs/latest`** now `LEFT JOIN app_route_map m ON m.route = c.route`
  and each capture carries `label`, `route_group`, `serving_file` (null when the map has no row —
  a capture is never dropped for lacking one). Order is `nav_position`, then route/theme/viewport.
  `EXPLAIN` on the real DB: nested-loop left join on `app_route_map_pkey`, index scan on
  `idx_visual_audit_captures_run`.
- **(b) `sync_route_map(conn, routes, run_id)`** runs at the start of every full crawl, on the SAME
  `parse_nav_routes()` output the crawl walks. One `INSERT … ON CONFLICT (route) DO UPDATE` per
  route, updating only what the nav itself carries: `label`, `surface` (flag `m` → mobile, else
  web — the exact split the table holds), `nav_flag`, `nav_position`. **Fable's columns
  (`route_group`, `serving_file`, `handler`, `template`) are never overwritten.** A route new to the
  nav is inserted with the literal sentinel `unmapped` in those four (template NULL),
  `added_by='visual_audit'`, and a WARNING naming it — Fable fills the mapping, the crawler never
  guesses a handler. A row whose route has left the nav is NOT deleted (the table is Fable's); it is
  listed as `stale` in the crawl summary (`route_map` key) and the log. Never raises — a map failure
  rolls back and the crawl continues.
- **Label decode fixed on the way:** the NAV sits inside `PWA_JS = """…"""`, a NON-raw Python
  string, so `'V9 \\u00b7 Pairs'` on disk is two escape layers deep. `parse_nav_routes()` now
  decodes one layer per pass (Python, then JS) so the label written is `V9 · Pairs` — the text the
  nav renders and the text Fable's rows already hold. Without this the first crawl would have
  overwritten Fable's `V9 · Pairs` with the escape sequence.

**Verify:** `ast.parse` clean. Local: 42 routes, `/v9` → `V9 · Pairs` / `◈`, flags m/d/None as in
the table; dry-run of the real `sync_route_map` with a fake connection (3 nav routes vs 3 existing
rows) → `upserted=3, new=['/m/home'], stale=['/gone-route']`, COMMIT last; a dead connection →
`error` set, ROLLBACK, no raise. The three EXACT rendered INSERT statements run on a scratch
`LIKE app_route_map INCLUDING ALL` table in the real Postgres (created, exercised, dropped): existing
`/` went `Home OLD`/pos 7 → `Home`/pos 1 with `core/main.py/home/scorr_home.html` untouched; `/v9`
kept `V9 · Pairs` and its mapping; `/m/home` landed `mobile/m/unmapped×3/visual_audit`;
`/gone-route` untouched. The live table was not written — that happens on the first real crawl.

## Fable verification of the other eyes commits (the founder's ask)

On `main` at `9881caa` (github_read against `refs/heads/main`): `visual_audit.py` sha `dbebf4a9`,
`visual_audit_endpoints.py` sha `49bf6f21`, `Dockerfile.visualaudit`, `railway.visualaudit.json` —
all present, byte-identical to the cc#2007/cc#2009 pushes; `main.py`'s include_router is in the
same commit. DB on real rows: 0 captures, 0 results (never run — correct), registry row present.
**Verdict:** cc#2007 and cc#2009 LANDED correctly. NOT LIVE: no service provisioned, no first run,
and the registry row had been auto-retired by cc#759 (root cause above, now fixed in both places).
cc#2012 supersedes cc#2009's Cron-Job service type and cc#2007's image-on-FAIL storage.

## First live run (12-Sep, after cc#2014 + cc#2015 fixed the boot) and cc#2016

The service came up after the tzdata + chromium-path + playwright-pin fixes (cc#2014, cc#2015).
Acceptance request (`/m/home`, aquawhite, mobile) went `done`, `capture_id=1`; the catch-up full
crawl then ran on its own (`last_run_at` was NULL, past 03:00 IST): run `5505ee536a344374`,
13:57:57–14:08:51 UTC, **168 captures = 42 routes × 2 themes × 2 viewports, 0 errors, 168/168 with
image bytes** (51 MB total, ~311 kB/JPEG). Full check tallies and the worst routes are in
`cc_task_logs` on cc#2012 (12-Sep). System is LIVE per rule 9.

**cc#2016 (P2)** — the founder pasted a capture URL with a valid token; Fable's own `web_fetch` can
read text/JSON/HTML but not a raw binary `image/jpeg` response, so the picture itself was still
unreachable to Fable even though auth and storage were both proven working. Fix, additive only, in
`visual_audit_endpoints.py`:

- `GET /api/visual-audit/capture/{id}?token=…&encoding=base64` — same `_token_ok` gate, same order
  (checked before `encoding` is even read, so there is no second weaker path) — returns
  `{"id","route","theme","viewport","mime","data_base64"}` instead of the raw image. `encoding`
  absent, empty, or any value other than `base64` (case-insensitive) is the **original path,
  unchanged** — a human opening the link still gets a normal image.
- No new column, no stored base64 copy — encoded from `image_bytes` at request time.
- `runs/latest`, the worker loop, the checks: untouched, per the card's own do-not-touch.

Verify (real function, fakes for the DB row, run in this container): wrong token → 404 in both
modes, **without querying the DB first** (checked directly — the auth gate raises before the
`SELECT`); default (`encoding=""`) → unchanged `Response`, same bytes/mime/`Cache-Control: no-store`;
`encoding=base64` → JSON, `data_base64` decodes to the exact stored bytes, `id/route/theme/viewport/
mime` correct; `encoding=BASE64` → same (case-insensitive); `encoding=bogus` → falls back to the
raw path unchanged; a purged/unknown row → 404 in either mode. 12/12 checks.

## cc#2018 (P2) — thumbnail mode: `?w=<int>` and `?crop=top`, composable with `?encoding=base64`

cc#2016 verified live: `encoding=base64` works end to end. The gap it exposed: a full capture's
base64 text (a 55KB JPEG ≈ 73K chars) is too large for Fable to reliably hand-copy out of its own
fetch context into a file. This card adds a downscaled/cropped re-encode on the SAME route.

**Pillow's availability — checked, not assumed (item 2's own gate):** `requirements.txt` pins
`weasyprint>=60,<63`; WeasyPrint's own PyPI metadata for 62.3 (the range's newest release) declares
`Pillow>=9.1.0` in `requires_dist`, unconditionally (no extra marker) — so `pip install -r
requirements.txt` already installs Pillow into the web service today, without this card adding a
new dependency. Item 3's fallback (a worker-side `thumb_bytes` column, gated by cc#351's ALTER
TABLE block) was therefore **not needed** and not built.

**What landed**, `visual_audit_endpoints.py` only:
- `?w=<int>` — aspect-preserving downscale to that width, **never upscales** (a `w` ≥ the stored
  image's width is treated as absent). `?crop=top` — keep only the first 915px of height (matches
  `visual_audit.py`'s own mobile `VIEWPORTS` entry) before any downscale; a page already ≤915px
  tall is left alone. Either one triggers a JPEG quality-60 re-encode; both compose (crop first,
  then downscale). Works in both the raw `image/jpeg` path and `encoding=base64` — in the latter,
  a produced thumbnail adds `width`/`height` keys to the JSON, present ONLY when a thumbnail was
  actually produced, so the pre-existing `encoding=base64` (no `w`/`crop`) response is unchanged.
- **No new column, nothing new stored** — built from `image_bytes` at request time, discarded
  after the response.
- **Degrades, never crashes**: a `w` that isn't a parseable positive int, Pillow being
  unimportable (defensive-only — confirmed present, see above), or stored bytes that fail to
  decode as an image all fall back silently to the **unchanged full image** rather than a 500.
  Logged once (not per-request) if Pillow is ever actually missing.
- Same `_token_ok` gate, same order, before any of the above is even reached — one auth path.

**Verify**, real function + fakes for the DB row, real Pillow-encoded JPEGs (this container, 18/18):
the full cc#2016 regression suite still passes (wrong token 404s without a DB hit; default path
byte-for-byte unchanged; `encoding=base64` with no `w`/`crop` has exactly the original five keys,
no `width`/`height`; purged/unknown row 404s in both modes) — proving cc#2018 changed nothing for
a request that doesn't ask for a thumbnail. New: `w=206` on a 412×3000 synthetic image → output
206px wide, height scaled proportionally (1500px, exact ratio); `crop=top` alone → height clamped
to 915, width untouched; `crop=top&w=206&encoding=base64` together → JSON `width=206`/`height=458`
(915 scaled by the same ratio as the width), `data_base64` decodes to a valid JPEG, 7,264 bytes —
comfortably under the ~20KB/16-20K-base64-char target; `w=9999` (bigger than the source) → no
upscale, width unchanged; `w=-5` and `w=notanumber` → ignored, no crash; Pillow forced unavailable
→ falls back to the full unchanged image, no crash.
