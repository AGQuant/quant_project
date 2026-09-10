# cc#1979 — API Endpoint Census (READ-ONLY)

No route was deleted, renamed, redirected or changed. This is a map only.

Method: every `.py` file in the repo (259 files, `research/`, `apps_script/`,
`design_refs/`, `previews/`, `docs/`, `migrations/`, `prompts/`, `tools/`, `lib/`,
`assets/`, `chat/` excluded from the walk but not from the consumer grep) was
parsed with `ast` for `@router.get/post/...` decorators, `APIRouter(prefix=...)`
assignments, and `include_router(...)` calls. `main.py`'s 137 `include_router`
calls (plus 2 nested ones — `news_endpoints.py` mounts `knowledge_endpoints.router`,
`scanner_endpoints.py` mounts `tc_lite_scanner.router`) were resolved against
those `APIRouter` prefixes to build the full mounted path for every route.
Consumers were found by literal + boundary-aware regex search of every `.html`,
`.js` and `.py` file for each route's path (path params like `{symbol}` become a
wildcard). Table sources were extracted by walking each route function's AST
(one level of same-file helper expansion) for `FROM/JOIN/INTO/UPDATE` targets.
Full per-route data is in `reports/CC1979_API_CENSUS.json` (676 rows).

## A — Route count from code, and the diff vs API_REFERENCE.md

- **676** `@router`/`@app` route definitions found in code, across **140** files.
- **670 MOUNTED** (reachable — their router is `include_router`'d in `main.py`,
  or the route sits directly on `app`). **6 DEAD_NOT_INCLUDED** — see below.
- **665 distinct mounted (method, path) pairs** from 670 definitions — one path
  is registered twice (see shadow route below).
- `API_REFERENCE.md` claims **187 rows / 146 distinct paths** in its own header
  text but the actual table rows in the file are **180 rows / 179 distinct
  paths** — the header's own count is stale.
- **4 doc rows have no route behind them in code at all**, and code confirms
  why in each case — this is drift, not a live gap:
  - `/api/digest/daily` — `main.py:1783` comment: *"are DELETED. Daily Digest
    V3 (digest_v3.py) is now the only digest."*
  - `/api/v8/buy_s1_bounce`, `/api/v8/sell_overbought` — both retired baskets.
    `v8_engine.py` comments confirm SO was retired 17-Jul-2026 and its funnel
    write was deleted; `buy_s1_bounce` is excluded from all P&L displays under
    rule 13 (V8_PNL_CANON_V1). The routes themselves are gone from code.
  - `/authdebug` — no matching route anywhere in code; a removed debug route
    the doc never got updated for.
- **485 mounted code paths have no row in the doc at all.** `API_REFERENCE.md`
  documents roughly **27%** of the live route surface (179 of 665 distinct
  paths). It is comprehensive for the original `main.py`-direct routes and the
  earliest mounted routers, and has not kept pace with everything mounted
  since — most of the `mobile_*`, `*_app_mobile.py`, `inv_scanner_*`,
  `tc_v4_*`, `screeners_*`, `gvm_*` and `v8_*` families barely appear.
  **This finding does not correct the doc** (out of scope per the card) — it
  records the drift so the founder can decide whether to regenerate it.

### Dead route (defined, never mounted)

`qsr_endpoints.py` (6 routes: `/api/qsr/funnel`, `/positions`, `/trades`,
`/preview`, `POST /run-scan`, `POST /run-exits`) is **not** wired — `main.py`
line 53 and 830 both comment it out: *"QSR RETIRED (founder order, session_log
33844) — unmounted, not deleted."* This is confirmed-dead-by-design, not a
defect — flagged only because the card asked for every defined-but-unmounted
route.

### Shadow route (same method + path registered twice)

`GET /api/v8/metrics/all` is defined **twice** — `main.py:1989` (direct on
`app`) and `v8_endpoints.py:1164` (`@router.get("/metrics/all")` under the
`/api/v8` prefix, included via `app.include_router(v8_router)`). Both are
reachable in the sense that both function bodies exist and are wired, but
FastAPI resolves a duplicate route by registration order — whichever handler
gets added to the app's route table first wins every request; the second is
dead code that never runs. **Which one wins is not established by static
reading alone** — it depends on the interleaved order of `main.py`'s own
`@app.get` decorators against its `include_router(v8_router)` call, which
this census did not resolve to a definitive answer. Both handlers build a
different SQL query (main.py's is simpler — no futures_universe/hourly_pct
join; v8_endpoints.py's is richer, matching the "Master tab" comment on the
sibling `/scan` route). **Risk:** whichever handler is actually dead is wasted
code that will confuse the next person who edits it expecting it to run.
Live consumers (`scorr_filters.html`, `scorr_v13.html`, `v8_dashboard.html`,
`mcp_dispatch.py`) are getting ONE of the two payload shapes today, silently.

## B — Duplicate families, canonical member, cost, risk

Ordered by what continuing to pay for both members costs today.

### 1. TC / Trade Check scoring — the founder's own example family

Seven tables have held "how good is this TC setup" at some point. Per
**TC_CANON_V2_FINAL** (session_log 41629, 08-Sep-2026) and the newer
**TC_LIVE_INTRADAY_CANON_V1** (session_log 42648, cc#1909, cited in
`v10_endpoints.py:1018`), the canonical intraday store is `tc_universe_ticks`
— V2 rules /100, all 207 futures symbols, all four buckets, every 5 minutes,
09:15–15:20 IST, never overwritten by the EOD run. **But three other jobs are
still ACTIVE and still feeding live surfaces today**, which is exactly the
"paying twice for the same answer" pattern the founder named:

| Store | Writer job (scheduler_master) | Cadence | Rows today | Consumers (live) | Canon status |
|---|---|---|---|---|---|
| `tc_universe_ticks` | `bg_tc_universe_tick` (active) | 5-min, market hours | 119,232 total, newest 09:50 UTC today | `mobile_home2.py` (Home TC basis), `v10_endpoints.py:1018 /api/v10/tc_screen`, `tc_v4_dual.py`, `v8_dashboard.html` | **CANONICAL** — session_log 41629 + 42648 |
| `v8_tc_score_ticks` | `bg_tc_score_tick` (active, `v8_pivot_star.run_tc_score_tick()`) | 5-min, market hours, **book-scoped only (~36 open-position symbols)** | 9,765 rows, newest today 15:20 | `GET /api/v8/tc_score_latest` (v8_dashboard.html TC% column) | Explicitly named **FORBIDDEN as a source** by TC_LIVE_INTRADAY_CANON_V1's own comment in `v10_endpoints.py:1018-1024` — yet its job is still active and still the only source for the dashboard's TC% column |
| `tc_position_stars_v2` | `bg_tc_position_stars_v2` (active) | once/5min-gridded but only at `m==30`, 09:00-15:00 IST | 2,069 rows, newest 15:30:48 today | `/api/trade-check/position-stars-v2` → `mobile/v8.html`, `trade_alerts_web.html`, `v8_dashboard.html`; `/position-stars/history` → `v8_dashboard.html` | Also named **FORBIDDEN as a source** by the same comment — also still active, still live-consumed |
| `tc_screener_cache` | `bg_tc_screener_precompute` — **`active=false`**, last ran 2026-09-08 16:00 IST | was nightly 16:00 | 33,600 rows, **frozen since 08-Sep** | `/api/mobile/check` (the **app's own Check tab**, `mobile/check.html`), `/api/trade-check/screen-cached`, `/movers`, `v12_endpoints.py` screener JOIN, `POST /api/admin/run-tc-screener` | Named **stale** by 41629 ("OLD /21 raw-filter scorer... the rule COUNT changed, so the two are not comparable at any scale") |
| `tc_cache` | none active (dead since 18-Jun-2026 per 41629, comment confirms) | — | 416 rows, frozen since 18-Jun | `intraday_scanner_endpoints.py` scanner joins, `previews/check.html` | **Dead**, per 41629 |
| `tc_position_stars` (v1) | `bg_tc_position_stars` — **`active=false`**, last ran 08-Sep 15:30 | was hourly-ish | 4,238 rows | `/api/trade-check/position-stars` → `v8_dashboard.html` | Unretired predecessor of v2 per 41629 — still live-consumed even though its job stopped |
| `tc_screener_v2` | `bg_tc_screener_v2` — **`active=false`** | was nightly 16:05 | 15,748 rows, frozen since 09-Sep | `POST /api/admin/run-tc-screener-v2`, `GET /api/trade-check/screen-v2` | Was the bridge scorer 41629 compared row-for-row before trusting it; superseded by `tc_universe_ticks` and no longer written |
| `v8_tc_score_daily` | `bg_tc_score_daily` — `active=false`, **never ran, 0 rows** | — | 0 | none found | Dead on arrival, per 41629 |

**The finding underneath the finding:** `/api/mobile/check` is the app's live
Check tab and its data source, `tc_screener_cache`, has had **no writer since
08-Sep-2026** (2 days as of this census) because `bg_tc_screener_precompute`
was deactivated. `mobile_endpoints.py`'s own comment on this route (line
~775-795, cc#887/888) describes exactly this failure mode happening once
already — it switched the route from `tc_cache` (50 days stale) to
`tc_screener_cache` for this reason, and promises *"Stale stated, never
dressed as fresh."* Whether that staleness-disclosure code still fires
correctly now that the **second** source has gone stale the same way was not
re-verified in this census — it is read-only and did not re-render the page.
This is the most consumer-visible item in the whole census and is stated
plainly as a **DECISION NEEDED** item below.

**Cost of running all four active TC jobs**: `bg_tc_universe_tick` 154ms/tick
(cheapest — batched), `bg_tc_score_tick` ~9.2s/tick (per-symbol loop over the
36-symbol book only), `bg_tc_position_stars_v2` ~8.8s at 09:00-15:00 IST on
the `:30` minute. None of the three non-canonical jobs are expensive in
isolation; the cost here is mostly **storage and consumer confusion**, not
compute: `tc_universe_ticks` alone is already 21MB/119K rows since 09-Sep and
growing at the sizing 41629 measured (61-78K rows/day across all four
buckets). Three more active writers on the same underlying question is
duplicated *engineering surface*, not duplicated *server load*.

**Risk of retiring the non-canonical three**: real, because each is the
*only* source for a specific live column today — `v8_tc_score_ticks` for the
dashboard TC% column, `tc_position_stars_v2` for the position-stars UI on
three surfaces, `tc_position_stars` (v1) still for one route on the
dashboard. TC_LIVE_INTRADAY_CANON_V1 forbidding them as sources for **new**
work (like the `/tc_screen` route it governs) is not the same ruling as
retiring what already reads them — that migration was not in scope for this
card and is not assumed done.

### 2. GVM screener — `v12_endpoints.py`'s `/screener` route joins a stale table

`v12_endpoints.py:108`'s `_BASE_SQL` (the `/screener` page's query, live-
consumed by `scorr_cio_dashboard.html`) does
`LEFT JOIN tc_screener_cache tc ON g.symbol = tc.symbol AND tc.run_date =
(SELECT MAX(run_date) FROM tc_screener_cache)` — the same table found stale
above. So this is not an isolated TC-tab problem: the GVM screener's TC
column is reading the same frozen-since-08-Sep table. **Same root cause as
family 1, same decision needed.**

### 3. Investment Check — three routes, three ages, same question

`/api/check` with `side=INVEST` (delegates to `investment_check.compute_invest_check`,
`check_endpoint.py:32`), `/api/investment-check*` (`investment_check.py`,
four routes reading `gvm_history/gvm_scores/screener_raw/v8_metrics`), and
`/api/investment-check-v2*` (`invest_check_v2.py`, reads `ic_rule_weights`,
live-consumed by `scorr_check.html`) all answer "should I buy this for the
long term." `investment_check.py:376`'s own `/api/investment-check` route
comment/import shows `investment_check.py`'s composite delegates INTO
`invest_check_v2.py` already (`CODE[invest_check_v2.py]` consumer on that
row) — so v1 and v2 are not fully independent; v1 appears to be a thin
wrapper in front of v2 for at least one path. **Evidence for a canonical
member is incomplete** — this needs one more read of `investment_check.py`'s
body (not done in this census) before tagging a winner. Marked
**UNRESOLVED**, not guessed.

### 4. TC Lite (`tc_intraday_signals`) vs TC Check family — NOT a duplicate

`tc_lite_scanner.py` (mounted under `scanner_endpoints.py`, `/api/scanners/tc_lite*`)
writes/reads `tc_intraday_signals` — a **screener flag log** ("save one
signal per symbol/side/day," no score, no P&L). This answers a different
question ("did this symbol trip the 5-check gate today") than the TC scoring
family above ("what is this symbol's /100 score right now"). Recorded here
only to say explicitly: **this is not part of family 1** — a repeated `tc_`
prefix is not evidence of duplication on its own, per the card's own
SCALE-IS-NOT-IDENTITY rule.

### 5. Health — three separate meanings share the word, not duplicates

`/api/health` (liveness), `/api/health/report` + `/api/health/feeds`
(`main.py`, data-freshness diagnostics), `/m/health` + `/api/mobile/health_app*`
(`health_app_mobile.py`, the Portfolio Health app feature reading
`hr_holdings/hr_ledger/hr_portfolios`), and `/api/health/report/{portfolio_id}`
+ `/api/health/generate` + friends (`hr_endpoints.py`/`hr_report.py`/
`hr_report_pdf.py`, the Portfolio Health Report engine) are **three unrelated
products** that all used the word "health." Not a duplicate family — recorded
to close the "repeated tail" candidate the spec flagged (`health` x6).

## C — Consumer classification (all 676 route definitions)

| Class | Count |
|---|---|
| LIVE-CONSUMED (found in an `.html`/`.js` served surface) | 347 |
| CONSUMED-BY-CODE-ONLY (only other `.py` files reference it — admin/internal/MCP-triggered) | 191 |
| **NO CONSUMER FOUND** | **138** |

Methodology caveat, stated plainly: this is a literal substring + word-boundary
regex search over the served-surface and code corpus, not a runtime trace. A
route called only from `apps_script/*.gs` (Google Apps Script, 4 files, outside
the repo's Python/HTML/JS grep set) or from an external caller (Railway cron,
a browser bookmark, Postman) would show as **NO CONSUMER FOUND** here without
actually being unused. `mcp_dispatch.py` alone accounts for 100 of the
CONSUMED-BY-CODE-ONLY rows — those are Claude/MCP-tool-triggered admin
routes, deliberately with no page.

### D — Routes with NO CONSUMER FOUND (138) and DEFINED-BUT-NOT-INCLUDED (6)

Full list is in the JSON (`consumer_class: "NO CONSUMER FOUND"`). By file,
the largest concentrations (candidate "cheapest to check next" list, per the
card's framing — cheapest-to-retire is NOT asserted here, only cheapest to
go verify by hand): `ops_metrics_pipeline.py` (14 — plausible, since
OPS_METRICS is founder-retired per CLAUDE.md and these are its now-dead admin
routes), `qb_endpoints.py` (11 — mostly admin/seed/propose routes, likely
MCP- or manual-triggered, not necessarily dead), `mf_pipeline.py` (9),
`main.py` (8, including `/status`, `/api/health` root liveness routes which
are legitimately unconsumed-by-a-page by design), `trade_check_v34_endpoints.py`
(7), `v12_endpoints.py` (7, the QB basket-builder CRUD API — likely called
from a page this grep missed, or from an external client), `qsr_endpoints.py`
(6 — the same routes already flagged dead-by-design above). The complete
138-row list with file:line is in the JSON; this census does not judge which
of the 138 are safe to touch — that is exactly the founder-gated sweep card's
job, with this map as its input.

## E — Cost / Risk note (per spec item 6-7, folded into family B above)

Per-family cost and risk are stated inline in section B for the one family
with real evidence (TC). The other candidate families flagged (`{symbol}` x8,
`{basket}` x5, `run` x5, `summary` x4, `tick` x3, `status` x3, `positions` x3,
`trades` x2, `pcr` x2, `intraday` x2, `adr` x2, `chat` x2) were spot-checked
during this census (see the PCR/ADR/positions/trades tables pulled in the raw
data) and **did not turn up a second duplicate family with the same strength
of evidence as TC** — they are mostly the same route family split across
`/v8`, `/v10`, `/v14`, `/qb`, `/v9` product lines that legitimately track
separate baskets/engines (a `/api/v14/positions` and a `/api/qb/positions`
are not duplicates of each other; they are different books). Flagging this
honestly rather than forcing a second family to satisfy the checklist: **TC is
the one big proven duplicate family in this pass.** A deeper pass (grep every
route body for its actual table, not just the ones already spot-checked here)
would be needed to rule the rest in or out with the same confidence, and is
scoped to a follow-up census, not asserted here.

## F — What this card did NOT do

No route was touched, no table was dropped, no writer job was
enabled/disabled, and `API_REFERENCE.md` was not corrected. Those are the
sweep card's job, gated on the founder's ruling on the DECISION below.
