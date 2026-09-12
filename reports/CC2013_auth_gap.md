# cc#2013 — AUTH GAP: four live pages with no gate, and the site password in a public repo

P0 (live exposure, nothing "broken"). Fable audit 12-Sep-2026, found while provisioning the
visual-audit service: `SCORR_AUTH_PASSWORD` is NOT set on the web service, so `scorr_auth.py` has
been serving every login on its in-repo fallback constant — and the repo is public.

## What this push lands (items 1, 3, 4). Item 2 is GATED, see below.

**Item 1 — the four routes are gated** (`main.py`, next to the existing `PROTECTED.add()` lines,
each with a `cc#2013` comment): `/quant-basket`, `/v10`, `/intraday` (web, after `/intel`) and
`/m/gvm2` (app, after `/m/v10`). Gate only — `_PWA_INJECT_PATHS`, the NAV array and
`NAV_REGISTRY` are untouched, as the card's do-not-touch requires.

**The set difference, re-derived by CC, not taken from the card.** Base set parsed from
`scorr_auth.py` with `ast.literal_eval` (12 routes) + every `PROTECTED.add("...")` in `main.py`
(45 before this card; no other module in the tree mutates PROTECTED — grepped) = 57. Against
`app_route_map WHERE crawl = true` (56 rows; `/m/login` is the 57th row and is `crawl=false`),
query strings stripped (`/cio2?model=gvm` → `/cio2`), minus `PUBLIC_PATHS`:

```
UNGATED = ['/intraday', '/m/gvm2', '/quant-basket', '/v10']
```

Exactly Fable's four. **No other ungated route exists** as of this push. Informational, the other
direction: `/ask`, `/digest`, `/filters`, `/m/qb/holdings`, `/v14` are in PROTECTED but have no
`app_route_map` row (redirects, sub-pages, or pages the map does not list) — harmless, gated anyway.
After this push the computed set difference is **EMPTY** (test B below).

**Item 4 — the startup assertion** (`scorr_auth.audit_gate_coverage()`, called from
`scorr_auth`'s own `_startup` handler, so `main.py` stays wiring-only). Reads
`app_route_map WHERE crawl = true`, strips query strings, subtracts `PROTECTED` and `PUBLIC_PATHS`,
and logs ONE line either way:

- gap: `ERROR scorr.auth: AUTH GATE GAP (cc#2013): 4 page route(s) in app_route_map are served with NO password gate -- not in PROTECTED, not in PUBLIC_PATHS: ['/intraday', '/m/gvm2', '/quant-basket', '/v10']. Add each to PROTECTED in main.py (or to PUBLIC_PATHS if it is public on purpose).`
  (this is the line as it would have read on the day `/quant-basket` shipped)
- clean: `INFO scorr.auth: gate coverage OK (cc#2013): all 56 crawl routes in app_route_map are PROTECTED or PUBLIC_PATHS`

Never raises; a DB blip logs a WARNING and returns `[]`. It runs after every module-level
`PROTECTED.add()` in `main.py` has executed (startup events fire after import), so it sees the
complete runtime set. New constant `PUBLIC_PATHS = {"/login", "/logout", "/m/login"}` — the
routes that are public on purpose (`/m/login`: cc#874 item 7, a login page behind the login gate
is a lockout). Today it only feeds the audit; it does not drive the gate.

**Item 3 — proposal (NOT implemented; founder decision).** Root cause is the exact-match design: a
new page is ungated by default and nothing complains. Proposed replacement, fail-closed:

```
gate = (path is NOT in PUBLIC_PATHS) and (path does not start with a PUBLIC_PREFIX)
PUBLIC_PATHS    = {"/login", "/logout", "/m/login"}
PUBLIC_PREFIXES = ("/api/", "/static/", "/pwa.js", "/sw.js", "/manifest", "/icons/", "/favicon")
```
i.e. every route is gated unless explicitly public; `/api/visual-audit/capture` and `/runs/latest`
stay outside (they are `/api/` and token-gated by design, cc#2012); `/preview/*` is gated by
construction instead of by the cc#866 prefix special-case. What it changes for the operator: a new
page needs NO `PROTECTED.add()` any more — forgetting the line now fails safe (page asks for the
password) instead of open. What it risks: any HTML or asset route that is public today by accident
of not being in PROTECTED starts asking for a password — the switch needs one pass over the app's
non-`/api/` routes to fill `PUBLIC_PREFIXES` (the `_PWA_INJECT_PATHS`/logout-button injection keys
stay on membership sets, unchanged). Recommended sequencing: land the audit first (this push),
watch one boot log, then a separate card flips the gate with the prefix list filled from the
route census (`reports/CC1979_API_CENSUS.md`). Until then the audit is the safety net.

## Item 2 — GATED, not pushed

The card's own gate: deleting `_PASSWORD` before `SCORR_AUTH_PASSWORD` is set on the WEB service
locks the founder out. Logged as a `QUESTION:` in the room; CC pushes the delete only after a
founder/RECO line confirms the env var is set on the web service **and** on the visual-audit
service (the crawler logs in with the same password, cc#2012). The delete will make `_password()`
raise `RuntimeError` when the var is unset — every login then fails loudly, no page is ever served
on an in-repo secret. Until it lands: `_PASSWORD` still exists at `scorr_auth.py` (two references,
tree-wide grep), and its literal value appears nowhere else in the repo.

## Verify — done here

- `ast.parse` clean: `main.py`, `scorr_auth.py`.
- `grep cc#2013 main.py` → the four adds (lines 301-303, 317).
- Real `audit_gate_coverage()` against a fake `app_route_map` cursor holding the 56 real routes:
  A base set only → 46 ungated (includes the four); B runtime set (base + the 49 parsed adds) →
  `[]` and the INFO line above, `/cio2?model=gvm` counted as `/cio2`; C runtime set minus the four
  → exactly `['/intraday', '/m/gvm2', '/quant-basket', '/v10']` and the ERROR line above; D DB
  down → `[]` + WARNING, no raise. 7/7.

## Verify — not done here

The boot log line on the real web service (this container cannot read Railway logs) — Fable reads
the first deploy after this sha for the `gate coverage OK` line. The browser check that the four
URLs now redirect to `/login` when logged out.

## Out of scope, restated

Password rotation and making the repo private — founder, Railway console / GitHub settings.
