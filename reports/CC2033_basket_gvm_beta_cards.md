# cc#2033 (P1) — ENGINE+UI: Basket GVM + Basket Beta on basket-list cards

Built under explicit founder authorization (Fable unavailable, session-standing "clear the queue
push all to main, founder decision"). Founder request 13-Sep-2026, live `/baskets` screenshot
supplied: each basket card should show its GVM score and beta up front, next to return%, not
buried in a drill-in view. Directly builds on cc#2032 (basket_beta is a straight read of that
card's own stored output, per this card's own spec).

## A real data-source correction, found before building

The spec named `quant_basket.gvm_score` as the per-stock GVM source. Checked directly: **`quant_basket`
has 0 rows in production** — completely empty. The real, live per-stock GVM source is `gvm_scores`
(1793 scored symbols, `score_date` fresh as of 2026-09-14) — confirmed by joining it against every
currently-held symbol across all 13 baskets: every real stock holding has a score; only
`GOLDBEES`/`SILVERBEES`/`MID150BEES` (gold/silver/mid-cap ETFs, not scored equities) come back
null, which is correct — GVM scoring does not apply to an ETF. Built against `gvm_scores`, not the
spec's literal (but empty) source, and stated here per Role Split's "CC greps and answers before
the build proceeds."

## Design decisions ("CC's call", per the card's own spec)

**Storage — `qb_gvm_daily`, a sibling table, not a `qb_beta_daily` column.** The spec asked to
"keep basket-level beta and basket-level GVM in the SAME row/table" and offered either as CC's
call. `qb_beta_daily` is a table CC created **this same session, minutes earlier**, already holding
one real production write (cc#2032's first run). An ALTER TABLE to add a `gvm` column would hit
the exact MAINTENANCE_LOCK_RULE (cc#351) gate cc#2032's own report cited for `qb_nav_daily` —
Railway-console-only, weekends, propose-first, not available today. `qb_gvm_daily` mirrors
`qb_beta_daily`'s shape exactly (`basket_name, nav_date, <metric>, n_holdings_used,
n_holdings_excluded, computed_at`, same PK) and the two are joined at READ time in
`performance_endpoints.py`/`qb_app_mobile.py` — satisfying "read both with one query" via a SQL
join across two sibling tables sharing `(basket_name, nav_date)`, not literally one row.

**Weighting — one shared function, not "the same logic re-typed twice."** The spec's own
instruction: *"identical weighting source/logic as cc#2032's basket_beta... do not invent a second
weighting path."* `_basket_weights(cur, basket_name)` is extracted verbatim from cc#2032's original
inline weight computation and is now the ONE function both `compute_all_basket_betas` (existing,
refactored to call it) and the new `compute_all_basket_gvm` call — guaranteeing the two rollups
cannot silently drift onto different weight bases in the future, not just today. Because this
touches already-shipped, already-verified code, the refactor was re-verified against cc#2032's own
real production output before shipping (see Verify below) — zero behavioural change.

**Per-holding exclusion, same convention as beta.** A holding with no `gvm_scores` row (an ETF, or
in principle a brand-new listing not yet scored) is excluded from both the numerator and
denominator of the weighted average, its weight redistributed proportionally across the rest —
never a fabricated GVM of 0 or 5 (the platform's blank-score sentinel elsewhere) for it.

## What changed

- **`beta_engine.py`**: `_active_baskets()` + `_basket_weights()` extracted (shared by both
  rollups); `compute_all_basket_gvm()` (new, mirrors `compute_all_basket_betas`'s shape exactly,
  scores against `gvm_scores`); `run_basket_gvm_engine()`; `qb_gvm_daily` table; a SECOND,
  independent gated trigger (`app_config['basket_gvm_run']`) + `/api/admin/basket_gvm/{run,status}`
  — independent of beta's trigger because the two now run on different schedule slots (see below)
  and must be able to fire without blocking each other.
- **`scheduler.py`**: `_bg_basket_gvm()` scheduled **01:38 IST** — AFTER the 01:30 GVM recompute
  (`_bg_gvm`) so `gvm_scores` is fresh for today, unlike beta (01:20, no `gvm_scores` dependency,
  left completely untouched by this card).
- **`performance_endpoints.py`** (web, `/api/performance/qb` — the actual data source behind
  `quant_basket.html`'s card grid, traced via `fetchWithTimeout` calls in that page, not the
  `qb_app_mobile.py` list endpoint originally guessed): each basket dict gains `basket_beta`,
  `basket_gvm`, and both metrics' used/excluded counts — a read-only `DISTINCT ON` latest-row join
  against `qb_beta_daily`/`qb_gvm_daily`.
- **`qb_app_mobile.py`** (app surface, CC_DEFAULT_BUILD_RULE_V1 — no pusher named, CC builds both):
  `_card()` (feeds `mobile_qb_list`, the app's own basket-list deck) gains `basket_beta`/`basket_gvm`
  the same way.
- **`quant_basket.html`** (web card face): two new tiles, **GVM** and **Beta**, added to the
  `.bc-metrics` grid immediately after **Return** (before Alpha N500/P&L/Mkt Value) — same visual
  tier, same tile styling, neutral colour (beta has no inherent good/bad direction the way a
  return does, so it is never given `c()`'s green/red treatment). `—` when null, never a fabricated
  placeholder. `.bc-metrics` is a 2-column CSS grid that auto-flows; 6 tiles instead of 4 needs no
  CSS change.

**App-side visual rendering — stated honestly, not overclaimed.** No live app-specific HTML
template exists in this repo for the `/api/mobile/qb_app/list` card grid (only
`design_refs/scorr_app_quantbasket_R5.html`, Fable's static design reference) — the native/PWA
client that renders that JSON is outside this repo's scope. This card's app-side deliverable is
the JSON fields (`basket_beta`/`basket_gvm` now present in the API response); the app's own
rendering of them is not something CC can build or verify from here. The web page
(`quant_basket.html`) is fully built and DOM-checkable, per below.

## What did NOT change

`quant_basket` (empty, unused — confirmed, not written to). `quant_paper_positions`,
`quant_basket_config`, `quant_basket_registry` — read-only sources. Existing basket-list fields
(name, description, return%, benchmark line, min-buy, next-rebalance) — additive only, none
replaced or reordered relative to each other. Rebalance/weighting logic — both numbers are display
reads from current weights, not fed back into rebalancing. `beta_daily`, `compute_all_stock_betas`,
`run_beta_engine`, the 01:20 beta schedule slot — untouched.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `beta_engine.py`, `scheduler.py`,
`performance_endpoints.py`, `qb_app_mobile.py`; `node --check` clean on `quant_basket.html`'s
inline script (all 4 script blocks extracted and checked).

**Refactor regression check — the load-bearing one, since this touched already-shipped code.**
Fetched the REAL `beta_daily` scores for all ~85 currently-held symbols directly from production
(the table cc#2032 populated this session) and ran the REFACTORED `compute_all_basket_betas`
against them through a stub cursor. Result: **every one of the 13 active baskets' beta AND
n_holdings_used exactly matches cc#2032's own real, already-shipped production output** — e.g.
`large_cap` 1.0239/15, `alpha_multicap` 0.8452/15, `model_portfolio` 1.0197/20, `contra_value`
correctly `null`/0 (its only holding is 100% cash-parked). Zero regression from extracting
`_basket_weights()`.

**The card's own explicit verify, done with real data**: fetched the real `gvm_scores` for every
held symbol and manually recomputed `large_cap`'s holdings-weighted GVM from those scores + its
real 15 holdings' real `current_value` — **7.2231**, used=15, excluded=0 — matches
`compute_all_basket_gvm`'s own output exactly, digit for digit.

**ETF exclusion, proven on real holdings**: `finz_etf` (100% `GOLDBEES`/`SILVERBEES`/`MID150BEES`)
correctly returns `gvm: null, used: 0, excluded: 3` — no crash, no fabricated score. `finz_stable`
and `finz_wcb` (each holding exactly one of these ETFs alongside real scored stocks) correctly
score the stocks and exclude only the ETF — `n_holdings_excluded` matches the real ETF count
exactly in both cases.

**Weight-base identity, checked across all 13 baskets, not asserted**: for every basket,
`beta`'s `(used+excluded)` and `gvm`'s `(used+excluded)` both equal that basket's real non-cash
holding count exactly — direct, measured proof the two rollups share one weight base, not two
that happen to agree today.

**First-run evidence** (ENGINE_LIVENESS_RULE 13829): triggered via the gated
`app_config['basket_gvm_run']` mechanism immediately after this deploys; real row counts and the
computed GVM for all 13 baskets to follow as a report extension, not asserted here in advance.
`Arpit will eyeball the live /baskets screen` per the card's own verify note — CC's container has
no egress path to scorr.in to self-check the rendered page (established constraint this session).
