# cc#2210 — /m/health and /m/sector: a logged-out visit goes to /login, not to an error card

Follow-up to cc#2208 (mobile/portfolio.html). Same helper, same behaviour, two more pages. Client-side only;
`mobile_endpoints._guard` and its 401 body `{"error":"unauthorized","login_url":"/login"}` are unchanged.

## What changed

**mobile/health.html** — `unauth(d)` ported verbatim (detect `error==='unauthorized'` OR a present `login_url`;
`location.replace` on a `login_url` that starts with `/`, else `/login`; `GOING` one-shot so two fetches cannot
both navigate). It now guards:

| fetch | endpoint | guarded server-side today? |
|---|---|---|
| boot (list / report) | `/api/mobile/health_app/list`, `/report?pid=` | yes — `_guard` (health_app_mobile.py) |
| `parse()` | `POST /api/health/upload` | **no** — hr_endpoints.py has no auth guard on this route |
| `save()` | `POST /api/health/generate` | **no** — same |

The parse/save guards are additive and inert until those two web routes get a guard; the card asked for them and
they cost nothing. The boot fetch is the one the founder can hit today (typed URL or the Home-grid tile with an
expired session): it printed `Could not load: unauthorized` and stopped.

**mobile/sector.html** — the same helper, with one difference the card's evidence did not anticipate: the cc#2206
ladder checks `!r.ok` BEFORE the content-type gate, so a 401 never reached the `d.error` branch — it threw
`HTTP 401 application/json — {"error":"unauthorized",...}`, auto-retried once after 800 ms and then rendered a
Retry button that could never succeed. Same dead end, different path. The fix lets a **401 + application/json**
response through to `unauth()` ahead of the ladder; if the body is not the app's shape the ladder gets it back
(`throw` with the status + body snippet), so an odd 401 still shows the diagnosable error. Every non-auth failure
keeps the cc#2206 behaviour unchanged (JSON error → `Could not load: …` + Retry; HTML-200 → status + type + body
snippet, one auto retry, Retry button).

No return-to parameter, as cc#2208 established: the app login lands on /m/home (`login.html`'s hidden
`next=/m/home`; `login_get` reads no `?next=`).

## Harness (Playwright, 375×812, both pages served from the working tree, `/login` a stub) — 16 checks, PASS

| # | case | result |
|---|---|---|
| H1 | /m/health logged out (list) | lands on /login, one api call |
| H2 | /m/health?pid=5 logged out (report) | lands on /login via health_app/report |
| H3 | report returns `{"error":"no such portfolio"}` | `Could not load: no such portfolio` (fail() unchanged), no redirect |
| H4 | view=new, parse() gets the 401 body | lands on /login |
| H5 | view=new, parse ok, save() gets the 401 body | lands on /login |
| H6 | save() returns `{"error":"bad holdings"}` | `#save-out` shows the text, stays on the page |
| H7 | parse() returns `{"error":"no holdings parsed","rows":[]}` | inline text unchanged, stays on the page |
| S1 | /m/sector logged out | lands on /login, exactly one api call, no Retry button ever rendered |
| S1b | /m/sector?seg=Private Banks logged out | lands on /login via sector_app/segment |
| S1c | /m/sector?view=all logged out | lands on /login |
| S2 | `{"error":"boom"}` (200) | `Could not load: boom` + Retry; as-of `Not loaded`; Retry re-runs the same fetch |
| S3 | HTML 200 (deploy page) | `HTTP 200 text/html — <!doctype html><h1>Deploying</h1>` after one auto retry, Retry button |
| S4 | 401 with `{"x":1}` (not the app's shape) | ladder: `HTTP 401 application/json — {"x":1}` + Retry, no redirect |
| S5 | 401 with an HTML body | ladder: `HTTP 401 text/html — …` + Retry, no redirect |
| S6 | 200 list payload | table renders (2 rows) |

`node --check` on every inline script block of both files: OK. Harness file: scratchpad `cc2210_test.py`.

## Item 4 — the other /m/* pages that share the pattern (report only, not fixed here)

Method: every `@router` route whose handler calls `_guard(` / `_is_authed(` is a guarded endpoint; every
`mobile/*.html` that fetches one of them and has no `unauthorized` / `login_url` handling is listed. The /m/* HTML
shell itself is served without an auth check (`_page`), so each of these is reachable logged-out by typed URL,
by a Home-grid tile, or with an expired session — and none redirects. 21 pages besides the three now fixed
(portfolio cc#2208, health + sector here):

**A. Prints the 401 body's `error` text as a data error (the exact cc#2208 shape) — 13 pages**

| page | guarded endpoint(s) it prints `d.error` from |
|---|---|
| mobile/dash.html | /api/mobile/myportfolio |
| mobile/fpc.html | /api/mobile/fpc/calc |
| mobile/invscan.html | /api/mobile/invscan |
| mobile/learn.html | /api/knowledge/articles |
| mobile/mf.html | /api/mobile/mf_app/list, /fund |
| mobile/myalerts.html | /api/mobile/myalerts |
| mobile/myportfolio.html | /api/mobile/myportfolio |
| mobile/qb.html | /api/mobile/qb_app/list |
| mobile/qb_holdings.html | /api/mobile/qb_app/holdings |
| mobile/results.html | /api/mobile/results_app/season, /companies, /analysis |
| mobile/screeners.html | /api/mobile/screeners_app/list, /screen; /api/mobile/custom_screener/meta, /run, /save, /saved |
| mobile/tcscan.html | /api/mobile/tcscan |
| mobile/trade_wall.html | /api/tradewall, /approved/levels, /prefill-levels (some handlers print `d.error`, others a generic catch text) |

**B. Generic catch text only ("Could not load …" with no body) — the 401 is swallowed into a generic failure — 6 pages**

| page | guarded endpoint(s) |
|---|---|
| mobile/home.html | /api/mobile/home2, /api/mobile/breadth (three catch blocks; the shell keeps its last-known cards) |
| mobile/digest.html | /api/mobile/digest (failBox) |
| mobile/holdings.html | /api/mobile/portfolio |
| mobile/models.html | /api/mobile/models |
| mobile/positions.html | /api/mobile/v8_positions |
| mobile/mywatchlist.html | /api/mobile/watchlists (`.mw-err` "Could not load…"; the add/remove posts show `e.message`) |

**C. No error branch at all — the page stays on its loading/empty state — 2 pages**

| page | guarded endpoint(s) |
|---|---|
| mobile/aicio.html | /api/mobile/aicio_app/cards |
| mobile/v8.html | /api/mobile/v8book, /api/mobile/v8funnel (catch sets the slice to null and re-renders empty) |

Pages not listed (alerts, check, gvm, intel, qbbuilder, options, …) either call endpoints that are not
`_guard`-ed or build their URLs in a way the grep could not resolve to a guarded route; they were not hand-audited.
The shared JS (scorr_appshell.js, scorr_card_common.js, scorr_mobile_cards.js) has no 401 handling of any kind, so
nothing catches this centrally — the site-wide interceptor the card lists as out of scope is the real fix for A+B+C;
until then each page needs the eleven-line helper.

## Live check (the sandbox cannot reach scorr.in)

Logged out: open /m/health and /m/sector — both should land on /login. Logged in: /m/sector?view=all still
renders, and a deploy-window 502 still shows the cc#2206 status text with a Retry button.
