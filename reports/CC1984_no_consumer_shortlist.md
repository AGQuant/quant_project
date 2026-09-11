# cc#1984 item 5 — the 138 no-consumer routes: a shortlist to verify, and nothing retired

**Nothing is retired by this card and nothing here should be retired without the hand check
described below.** The card says so and the census says so, and the reason is worth restating:
the census is a regex grep over this repository. A route called from `apps_script/*.gs`, from a
Railway cron, from a monitor, or by any external client looks unused while being perfectly live.

## What the census said, and whether it holds up

Of 676 rows: 347 LIVE-CONSUMED, 191 CONSUMED-BY-CODE-ONLY, **138 NO CONSUMER FOUND**.

I narrowed those 138 to the ones where retiring could not silently break a write or a page:
**mounted**, **GET**, `writes=false`, `returns_html=false` → **63 routes**.

I then re-ran the consumer check myself, boundary-aware, over every `.html`, `.js`, `.py`, `.gs`,
`.json` and `.css` in the tree, excluding each route's own file, excluding documentation (a doc is
not a consumer), and excluding lines that are route **declarations** in other files (`@router.get(...)`
whose relative path happens to end in the same segment).

**All 63 come back with zero callers.** The census's grep holds up. Three notes on getting there,
because each was a wrong answer first:

- Without a **leading** boundary, `/status` matched inside `/api/paper/status` 31 times.
- Counting `API_REFERENCE.md` as a consumer made every route look used — my own item-3 work had
  just written all 666 of them into that file.
- Counting another module's `@router.get("/status")` as a consumer confused a **declaration** with
  a **call**.

## Seven routes are excluded from any shortlist, permanently

These have no caller in this repo **by design** — they are called by infrastructure, by browsers,
or by a protocol spec, and nothing in this codebase will ever name them:

| Path | Who calls it |
|---|---|
| `/api/health` | Railway health check, uptime monitors |
| `/api/scorr/health`, `/api/health/memory` | same |
| `/status` | same |
| `/manifest.json` | every browser that installs the PWA |
| `/.well-known/oauth-authorization-server` | an MCP client's auth discovery — the path is fixed by the OAuth spec and constructed by the client |
| `/.well-known/oauth-protected-resource` | same |

Retiring any of these because a grep found nothing would break the platform. They are named here so
nobody rediscovers them on the list and wonders.

That leaves **56** to consider.

## The ranked shortlist

Ranked by how *unlikely* a hidden caller is. A route scores up for a named successor that IS
consumed, or for belonging to a system the founder has already retired; and down for a
health/status shape, a path parameter (more likely built dynamically), or an `/api/admin` prefix
(may be called by hand or from a runbook).

### Tier 1 — verified by hand, a successor is named and live

| # | Path | File | Why it is a candidate |
|---|---|---|---|
| 1 | `/api/mobile/home` | `mobile_endpoints.py` | **Superseded by `/api/mobile/home2`**, which the mobile pages reference 6 times. `mobile/home.html` calls `home2`, `home/approved-trades` and `home/derivatives` — never the bare path. |
| 2 | `/api/mobile/v8` | `mobile_endpoints.py` | **Superseded by the `v8book` / `v8funnel` / `v8_positions` family**, all referenced by `mobile/v8.html`. The bare path is referenced nowhere. |
| 3 | `/api/mobile/qb` | `mobile_endpoints.py` | **Superseded by the `/api/mobile/qb_app/*` family** (`list`, `holdings`), which `mobile/qb.html` and `mobile/qb_holdings.html` call. The bare path is referenced nowhere. |

These three were checked by reading what the mobile pages actually fetch, not by scoring. They are
the strongest candidates on the list.

### Tier 2 — a system the founder has already retired

Ops-metrics is **FULLY RETIRED** (founder 09-Aug-2026, session_log 18213: "do NOT drain
`ops_metrics_t1_queue`, ignore any `ops_metrics_pending` signal, cc#768 cancelled, no new
ops-metrics tasks"). Its read endpoints are still mounted and answer nobody.

| # | Path | File |
|---|---|---|
| 4 | `/api/ops_metrics/registry` | `ops_metrics_pipeline.py` |
| 5 | `/api/ops_metrics/sector_trend` | `ops_metrics_pipeline.py` |
| 6 | `/api/ops_metrics/concall/{symbol}` | `ops_metrics_pipeline.py` |
| 7 | `/api/ops_metrics/guidance/{symbol}` | `ops_metrics_pipeline.py` |
| 8 | `/api/admin/ops_metrics/storage` | `ops_metrics_pipeline.py` |
| 9 | `/api/admin/ops_metrics/status` | `ops_metrics_pipeline.py` |

The retirement decision is already made; what has not been decided is whether the routes go too.
That is a founder call, not mine, and 8 and 9 carry an admin prefix so a runbook may still hit them.

### Tier 3 — no successor found, no protective signal; verify by hand

| # | Path | File |
|---|---|---|
| 10 | `/api/bt6/runs` | `bt6_endpoints.py` |
| 11 | `/api/bt6/trades` | `bt6_endpoints.py` |
| 12 | `/api/delivery/series` | `deriv_metrics.py` |
| 13 | `/api/intraday/dashboard` | `trade_check_v34_endpoints.py` |
| 14 | `/api/investment-check-v2/batch` | `invest_check_v2.py` |
| 15 | `/api/investment-check-v2/weights` | `invest_check_v2.py` |
| 16 | `/api/investment-check/screener` | `investment_check.py` |
| 17 | `/api/investment-check/summary` | `investment_check.py` |
| 18 | `/api/max/ivr/taps/summary` | `max_ivr_endpoints.py` |
| 19 | `/api/mobile/sector/detail` | `mobile_ext.py` |
| 20 | `/api/mobile/v8lower` | `mobile_ext.py` |

`/api/mobile/gvm` sits just outside the twenty: it has **no** sibling in any mobile page, which is
either a strong signal or a sign that `mobile/gvm.html` reaches its data by a route outside the
`/api/mobile/` family. That is a five-minute check and it should be done before it is ranked.

## The hand check each one still needs

A route earns retirement only after all four:

1. `apps_script/*.gs` and any Google Sheets bound script — the census's own named blind spot.
2. The Railway cron and scheduler job definitions — a job that curls an endpoint leaves no trace in
   this repo.
3. Server access logs for the path over a meaningful window. This is the only evidence that settles
   it, and it is the one thing neither the census nor I can see from here.
4. If it survives all three: **retire it to a 410 with a pointer, keep the route wired** — the
   pattern used on `/api/mobile/check` in this same card. An unknown caller then gets a clear
   answer instead of a 404, and the decision is reversible.

Retirement itself remains a separate founder-gated card.
