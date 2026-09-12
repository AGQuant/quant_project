# cc#2008 — Consolidation Inventory (READ-ONLY census, session_log CONSOLIDATION_INVENTORY_V1)

Founder direction 12-Sep-2026 ~08:50 IST. **Nothing in this app was changed to produce this
report.** No route merged, renamed, deleted or deprecated; no file moved; no table touched. Every
finding below carries file:line evidence on every side of every claim, per the card's own bar: "a
claim without file evidence is not a finding."

**This is not a blank-page census.** `reports/CC1979_API_CENSUS.md` (+ its `CC1979_API_CENSUS.json`,
676 rows) already did the bulk of the route/duplicate-family mapping this card asks for, and
`reports/CC1983_TC_CONSOLIDATION_PLAN.md` already carries a founder ruling on the single biggest
family (TC scoring). This report verifies those numbers are still current, finds three concrete
generator bugs, and adds what neither prior document covers: **live, evidenced violations of
CLAUDE.md's own founder-locked V8_PNL_CANON_V1 rule**, a second PCR-drift family, and an
overlapping-health-endpoint family.

## ⚠ Two findings meet the card's own gate ("a clash actively serving wrong data — STOP and report
as P0") — read this section first

### P0-A: V8_PNL_CANON_V1 (CLAUDE.md rule 13, founder-locked) is violated by FOUR live endpoints today

Rule 13 states, verbatim: *"there is exactly ONE V8 book formula, served from ONE endpoint,
consumed by EVERY surface... NO surface recomputes book P&L or win rates locally."* The canon is
`v8_book_canon.py` (`GET /api/v8/book_canon`) — wins = `result='TARGET'` only, rate over *decided*
trades only (never all trades), retired baskets excluded via the `app_config` registry, dash never
0%. Correctly consumed by `main.py`'s own summary helpers and `mobile_ext.py` (whose own comment
says "It computes nothing now" — a surface that was already fixed).

Four endpoints still compute their own, mutually-disagreeing win-rate/P&L today:

| Endpoint | File:line | Win definition used | Retired-basket exclusion? | Era cutover? | Brokerage deducted? |
|---|---|---|---|---|---|
| `GET /api/health/report` | `main.py:1753-1756` | `result='TARGET'` ÷ **all** trades (not decided) | **No** | No | No |
| `GET /api/diagnosis` | `diagnosis.py:279-287` | Byte-for-byte copy of the same bug | **No** | No | No |
| `GET /api/v8/replay/summary` | `v8_replay_endpoints.py:58-64` | `pnl > 0` — the exact definition `v8_book_canon.py:328-332` names as *discredited*, citing up to a 14-point spread on one basket | **No** | No | No (raw gross) |
| `POST /api/scorr/chat` (native, "paper summary") | `native_router.py:824-839` | `result IN ('TARGET','GAP_TARGET_EXIT')` — a **third** win definition | **No** | No | No |

All four query `v8_paper_trades` directly rather than calling `v8_book_canon.book_canon()`. Given
CLAUDE.md's own words are that a CEO instruction "cannot make a false figure true" and a LIVE badge
must never run ahead of real data, four different win-rate numbers answering the same question from
the same table — one of them still counting the retired `buy_s1_bounce`/`s1_reclaim_obs` baskets
that rule 13 says must "vanish from all P&L displays completely" — is exactly the class of thing
that rule exists to prevent. **Not fixed here** (out of scope: "each merge becomes its own card").
Recommend a dedicated P0/P1 card, raised by the founder, pointing each of the four at
`v8_book_canon.book_canon()`.

### P0-B: three system-health endpoints independently grade the same tables with different thresholds and can disagree simultaneously

`GET /api/health/report` (`main.py:1691-1709`), `GET /api/health/feeds` (`main.py:1899-1929`) and
`GET /api/diagnosis` (`diagnosis.py:100-232`) each independently run their own `SELECT MAX(...)`
freshness check against the same 7+ tables (`raw_prices`, `gvm_scores`, `v8_metrics`, `v8_qualified`,
`global_indices`, `adr_daily`, `pcr_daily`, `futures_basis`) — with **different thresholds**: e.g.
`raw_prices` is `fail` in `/api/health/report` at >1 day old (`main.py:1693,1707`), but `stale` only
at ≥7 days in `/api/health/feeds` (`main.py:1938`). The same day's data can be reported failing by
one endpoint and healthy by another, at the same instant. Separately, `GET /api/health/feed`
(singular, `feed_health_endpoints.py:58`) and `GET /api/health/feeds` (plural, `main.py:1899`) name
a totally different mechanism (live tick-feed grading vs. table-freshness list) one character apart
in the URL — a standing confusion risk, not itself a data bug. **Not fixed here.**

---

## Item 1 — route enumeration (registry-derived, verified twice independently, then re-verified live)

Two independent full-repo scans (one per background agent, cross-checked against each other and
against a fresh, live run of this repo's own `tools/gen_api_reference.py --check`) agree:

- **70 direct routes** on `main.py`'s `app` (not the ~50 CLAUDE.md's own API Reference section
  states).
- **602 router-sourced endpoints** across **141 source files**, mounted via **140**
  `app.include_router(...)` calls in `main.py` **plus 2 more reachable only via nesting**
  (`news_endpoints.py` nests `knowledge_endpoints.py`'s router; `scanner_endpoints.py` nests
  `tc_lite_scanner.py`'s) — **142 router files total**, not the "28 mounted routers" CLAUDE.md's API
  Reference section currently states. `qsr_endpoints.py` (6 routes) is defined but deliberately
  unmounted (`main.py:53,831`, "QSR RETIRED, founder order, session_log 33844") — confirmed
  dead-by-design, excluded from the live count.
- **TOTAL LIVE ENDPOINTS: 672.** Zero true duplicate `(method, path)` pairs across the whole surface
  (one earlier-found "shadow route" from cc#1979, `GET /api/v8/metrics/all` defined twice, was
  re-checked this pass and is **still** defined in both `main.py:2006` and `v8_endpoints.py:1163` —
  unresolved since cc#1979, not newly found).
- **API_REFERENCE.md divergence, live-checked**: I ran `tools/gen_api_reference.py --check` myself
  this session — it currently reports **"generated block is OUT OF DATE."** The doc's own generated
  block (dated 11-Sep-2026 08:31 IST in its own text) claims 666 live paths; the true number today
  is 672. Diffing file-by-file found the doc's generator has **three concrete, fixable bugs**:
  1. `worker/fyers_hist_backfill.py` (4 routes) is **miscategorized as "defined but NOT mounted"** —
     it is actually mounted (`main.py:132` imports it via the `worker/` sys.path shim `main.py:13-15`
     adds specifically for this file; `main.py:912` includes it). The generator has no special-case
     for that shim path, which is the likely root cause.
  2. `option_chain_grid.py`'s route (cc#2004, mounted `main.py:891-892`) is **missing entirely** —
     added after the doc's last 11-Sep 08:31 generation run.
  3. `visual_audit_endpoints.py`'s route (cc#2007, mounted `main.py:893-894`) is likewise **missing
     entirely** — same reason.
  `666 (doc) + 4 (miscategorized) + 1 + 1 (missing) = 672 (actual)` — reconciles exactly. This is a
  **tooling** finding, not just a data-staleness one: `tools/gen_api_reference.py` needs the
  `worker/`-shim fix, and needs to be re-run to pick up cc#2004/cc#2007. Neither is done here (this
  card is read-only; touching the generator or re-running it to rewrite the doc is its own small
  follow-up card).
- CLAUDE.md's own `## API Reference` section (repo root, "50 direct routes + 100+ router endpoints
  across 28 mounted routers") is itself now materially stale against the numbers above — flagged as
  a finding, not corrected here (CLAUDE.md is out of this card's write scope by the same "read-only"
  principle, even though it isn't named in do_not_touch).

## Items 2-3 — duplicates and overlapping data paths

Full evidence trail (every claim below carries both/all endpoints and both/all source files with
line numbers) is preserved in this session's own working notes; the findings themselves:

**PCR** — at least **five** independent live computations of the same or closely related figure
from `option_chain`/`pcr_daily`, not sharing one function:
1. Three hand-copied SQL writers of `pcr_daily` (`scheduler.py:621-678`, `scheduler.py:1822-1860`,
   `pcr_backfill.py:241-272`), each re-typing the same CASE-expression formula literally rather than
   calling a shared one. **`pcr_guard.py`** (`pcr_guard.py:38-49`) exists specifically so "the three
   pcr_daily writers cannot disagree," but its own `guard_sql()` function is **never called anywhere
   in the repo** — only its separate `warn_nulled()` helper is used (`scheduler.py:672,1859`,
   `pcr_backfill.py:271`). The safety mechanism exists and is dead.
2. Two independent whole-chain live-PCR readers: `oi_structure.py:148-180` (feeds `/api/oi/structure`
   and, via that, `/api/mobile/home/derivatives`) and `pcr_mood.py:155-185` (feeds `/api/pcr/mood`) —
   `mobile_home_derivatives.py:17-24` itself documents these as "independently-existing composers...
   reconciliation at spec time... confirmed both read the same totals" — i.e. kept in sync by a
   one-time manual check, not by shared code.
3. A third, narrower ATM±3 PCR (`deriv_metrics.py:290-327`, `GET /api/deriv-metrics/{symbol}`) — a
   materially different window, not guaranteed to agree with #2.
4. A fourth, independent intraday-series PCR query (`mobile_home2.py:284-303`), for a legitimate
   reason (a time series, not a single value) but still a fourth re-derivation of the same ratio.
5. `native_router.py:978-985` (the chat's native path) reads `pcr_daily` directly for "the current
   PCR" — against `pcr_mood.py:129-137`'s own documented founder ruling (cc#1846, 08-Sep-2026) that
   neither `pcr_daily` nor `pcr_intraday` should be read for a live/current value any more.

**Max pain / OI walls** — `option_chain_grid.py`'s own claim to reuse `oi_structure`/`max_pain`
rather than recompute is **verified true** (no third payout-formula implementation found; this
session's own cc#2004 work is clean). However `GET /api/oi/structure` and `GET /api/v10/maxpain`
(`v10_endpoints.py:611-674`) independently fetch the chain and call `max_pain.max_pain()`
separately rather than one wrapping the other — the core math is centralized, but the
query-then-call orchestration is duplicated across two live, independently-callable endpoints.
`deriv_metrics.py:331-336`'s own ATM±3-windowed wall computation is a further, narrower, independent
formula reachable with any symbol including NIFTY/BANKNIFTY — flagged as a code-level overlap, not
a confirmed live discrepancy (no evidence found either way that the frontend actually calls it with
an index symbol).

**Trade Check / TC scoring** — **already fully documented** by `reports/CC1979_API_CENSUS.md`
section B.1 and ruled on by `reports/CC1983_TC_CONSOLIDATION_PLAN.md` (canon: `tc_universe_ticks`,
founder ruling "V2 everywhere"). Confirmed still current: 11 mounted routers still touch this
question, and `v8_tc_score_ticks` / `tc_position_stars_v2` are both still named **FORBIDDEN as a
source** by the canon's own comment (`v10_endpoints.py:1018-1024`) while still being the *only* live
source for the dashboard TC% column and the position-stars UI respectively. **This report adds
nothing new here** — cited so the founder does not re-litigate a family that already has a ruling.

**Health endpoints** — see P0-B above; a genuine overlap this session's own agents found that
`reports/CC1979_API_CENSUS.md` section B.5 explicitly ruled OUT as "three unrelated products that
share a word" — that ruling is still correct at the *product* level (system health / portfolio
health / live-feed health are genuinely different things), but it did not check whether the
system-health ones agree with EACH OTHER, which they do not (P0-B).

**Investment Check** — `reports/CC1979_API_CENSUS.md` section B.3 already flags this family as
**UNRESOLVED** (three routes, `investment_check.py` v3.0 canonical / `invest_check_v2.py` a later,
unrelated engine / `check_endpoint.py`'s `side=INVEST` delegate). Confirmed still unresolved this
pass; not re-litigated further here.

## Item 5 — router sprawl (full accounting, headline: the "28 routers" figure is off by 5x)

**142 router files** (140 `main.py`-mounted + 2 nested-only), **602 endpoints**, averaging 4.2/file.
**68 of 140 (49%) expose only 1-2 endpoints** — this is a stated repo *convention*
(`APP_CARD_LAYOUT_LAW_V1`, session_log 42536: "one new file + one new router per feature"), not an
accident, and explains most of the count on its own. Full per-router file:line:prefix:count table
and one-line ownership statement for all 142 files is preserved in this session's working notes
(too long to inline here in full — available on request); headline overlap flags:

- **`/api/admin/*` has no owning router** — at least 12 separate files each define their own
  `APIRouter(prefix="/api/admin")` or hardcode the path locally (`admin_data.py`,
  `admin_index_backfill.py`, `stock_options_backfill.py`, `fy_end_backfill.py`,
  `worker/fyers_hist_backfill.py`, `fundamentals_scraper.py`, `fyers_range_backfill_endpoints.py`,
  `github_ops.py`, `yahoo_symbol_resolver.py`, `scheduler_master.py`, `result_analysis_gen.py`,
  `client_index_endpoints.py`) — a namespace, not a module.
- **`/api/v8/backfill` is mounted from two separate files**: `v8_backfill_endpoints.py` (3 routes)
  and `v8_metrics_gapfill.py` (1 route) — two router objects on the same prefix rather than one.
- **Two unrelated features are both named "Investment Scanner"**: `scanner_endpoints.py`'s single
  old `GET /api/scanners/investment` route, and the 4-file `inv_scanner_*` engine
  (`inv_scanner_universe.py`+`_scoring.py`+`_rules.py`+`_endpoints.py`) serving `/inv-scanner` — same
  product name, unrelated code, different data, confirmed by reading both.
- **Two "marker/star" systems on V8 signals** roughly two cc#-generations apart:
  `v8_pivot_star.py` (cc#856) and `v8_marker_ticks.py` (cc#1978) — flagged as a candidate for a
  follow-up read (not fully opened this pass), not a confirmed duplicate.
- Ruled out, checked and confirmed NOT overlapping despite suggestive names: the 5 `gvm_*` routers
  (each a genuinely distinct job); `trade_check_v34.py`/`trade_check_v36.py` (both files' own
  docstrings + `test_tc_resolver_guard.py:40-84` confirm a deliberate side-by-side A/B design, not a
  fork).

## Item 6 — near-duplicate files

Deliberate web/app splits (CLAUDE.md's own Role Split: "WEB pages stay CC even when they look like
an app page") are **not** flagged as duplicates: `scorr_check.html`/`mobile/check.html`,
`scorr_health.html`/`mobile/health.html` (explicitly named an approved exception,
`APP_VS_WEB_AUDIENCE_SPLIT_V1`), and similar pairs. Genuine candidates, evidence-backed:

1. **`invest_check.py` / `invest_check_v2.py` / `investment_check.py` — a three-way naming trap.**
   `investment_check.py:1-31`'s own docstring says it **is** "v3.0," replacing both a prior
   `investment_check.py` v1.0 and an `invest_check.py` v2.0 — i.e. today's `investment_check.py` and
   today's `invest_check.py` are NOT the v1→v2 pair a reader would guess from the names.
   `invest_check.py:1-16` is now a 33-line dead re-export shim ("ARCHIVED/SUPERSEDED cc#588"), kept
   only so old imports don't break. `invest_check_v2.py:1-8` is a **third, unrelated, currently-live**
   engine (a different scoring philosophy, launched a month later under a different spec) — its "V2"
   is not a continuation of the retired `invest_check.py`'s old "v2.0." Both `investment_check.py`
   and `invest_check_v2.py` are live and separately mounted today.
2. **`mobile_home2.py` has no `mobile_home.py` — the real "v1" logic sits inside `mobile_endpoints.py`,
   which already 410s its own superseded route.** `mobile_endpoints.py:695-716`'s own
   `GET /api/mobile/home` returns HTTP 410 ("Superseded by /api/mobile/home2... ZERO CALLERS,
   re-verified") while that same file still owns the live `GET /m/home` page shell
   (`mobile_endpoints.py:723`). The page-serving code and the (now-v2) data-serving code for one
   screen live in two different files by historical accident.
3. **`screener.html` is confirmed dead weight.** `v12_endpoints.py:309-312`'s own 301-redirect
   comment says it was "kept one release for rollback, then deleted next" (cc#407) — a full-repo grep
   for `screener.html` outside that one comment returns nothing. It still sits at the repo root,
   confusingly close in name to the live, unrelated `scorr_screeners.html` (plural, predefined
   screeners feature).
4. **`scorr_digest_mobile_PIVOT_LINE_PREVIEW_v3.html` is an orphaned preview file, misfiled.** Its
   own `<title>` says "Preview v3 (SVG text, sample + stress data, not live)" — a narrow
   sub-component preview, not a fork of the real, live `scorr_digest_mobile.html` it shares a stem
   with. Zero routes serve it (grep-confirmed). Per CLAUDE.md's own Role Split, preview files belong
   in `previews/**` or the `design_refs/**` numbered chain — this one is neither, sitting at the repo
   root under a name that invites confusion with the production page.

## Item 4 — dead routes

`reports/CC1979_API_CENSUS.md` section C/D already classified all 676 routes it found: 347
LIVE-CONSUMED, 191 CONSUMED-BY-CODE-ONLY (admin/MCP-triggered, deliberately page-less —
`mcp_dispatch.py` alone accounts for 100 of these), and **138 NO CONSUMER FOUND**, with the caveat
stated plainly that a route called only from Google Apps Script or an external cron/bookmark would
show as "no consumer" without being unused. This session's own agents did not re-run that
grep-every-route pass (it is already done, at the same rigor this card would apply, in the same
repo) — re-doing it byte-for-byte was judged lower value than the new PCR/health/canon findings
above, given the effort budget for this pass. **Not re-verified this session; cited, not redone.**
Stated honestly as a coverage gap in this specific push rather than silently claimed as re-checked.

## Ranked consolidation candidates (highest value first — value = callers simplified × duplication removed)

1. **V8 P&L canon violations (P0-A above).** 4 endpoints, 1 founder-locked rule, real cited number
   disagreements (up to 14 points on one basket per the canon file's own comment), one endpoint
   still counting retired baskets against an explicit "vanish completely" rule. Fix: point all four
   at `v8_book_canon.book_canon()`. Breaks-if-careless: `/api/diagnosis`'s and `/api/health/report`'s
   own grading thresholds are tuned to their current (wrong) numbers and would need re-tuning
   against the canon's numbers, not just a function-call swap.
2. **TC / Trade Check scoring family.** Already ranked #1 by `reports/CC1983_TC_CONSOLIDATION_PLAN.md`
   with a founder ruling in hand ("V2 everywhere") — the largest true duplicate family in the repo
   (7 tables, 11+ routers), simply not yet executed. Breaks-if-careless: `v8_tc_score_ticks` and
   `tc_position_stars_v2` are each the *only* live source for a specific UI column today; retiring
   either without first repointing its one consumer breaks that consumer, not a hypothetical one.
3. **System-health endpoint disagreement (P0-B above).** 3 endpoints, 1 shared set of tables, 2
   different threshold schemes. Fix: one shared freshness-check function, called by all three with
   their own presentation/grading on top. Breaks-if-careless: the three currently serve different
   audiences with different tolerance for noise (a diagnostic dashboard vs. an automated alert) —
   unifying the CHECK should not force them to also share the same alert threshold.
4. **PCR computation drift.** 5 independent formulas, 1 already-built-but-unused guard
   (`pcr_guard.py`). Lowest-effort fix on this list: wire `guard_sql()` into the three `pcr_daily`
   writers that already exist to use it, then decide whether `oi_structure.py`/`pcr_mood.py` should
   converge onto one shared "whole-chain PCR" helper. Breaks-if-careless: `deriv_metrics.py`'s ATM±3
   PCR and `mobile_home2.py`'s intraday series are legitimately answering different questions and
   must not be forced onto the whole-chain formula.
5. **`/api/admin/*` namespace fragmentation.** 12 files sharing one URL prefix with no owning
   module. Lower urgency (no wrong-data risk, purely an engineering-surface cost) — a candidate for
   a future organizational pass, not urgent.
6. **Investment Check three-way naming trap.** Already flagged UNRESOLVED by cc#1979; renaming
   `invest_check_v2.py` to something that doesn't imply it succeeds the dead `invest_check.py` shim
   would remove the trap cheaply without touching either live engine's behaviour.
7. **Dead files**: `screener.html`, `scorr_digest_mobile_PIVOT_LINE_PREVIEW_v3.html` — zero
   consumers each, confirmed by grep. Lowest risk, lowest value (cleanup only, not a data-integrity
   fix); safe to delete whenever convenient.

## What this card did NOT do

No route merged, renamed, deleted or deprecated. No file moved. `API_REFERENCE.md` was not
regenerated (its generator has known bugs now, per Item 1 — fixing the generator and re-running it
is its own small follow-up, not folded in here). `pcr_guard.py` was not wired in. The V8 P&L canon
violations were not fixed. `reports/CC1979_API_CENSUS.md`'s own item 4 dead-route grep was not
re-run byte-for-byte this pass (cited, not redone — see Item 4 above). Performance and DB-schema
consolidation are explicitly out of scope per the card. Every consolidation named above becomes its
own separate card, raised by the founder, per the card's own stated principle.
