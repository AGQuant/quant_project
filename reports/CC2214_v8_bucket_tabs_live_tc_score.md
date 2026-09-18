# cc#2214 — /v8 four bucket tabs: TC /100 resolved live for every qualified row

Spec: founder app comment 18-Sep, diagnosed live against the Railway DB. Screenshots at 15:05 and
15:10 showed INFY and KPITTECH blank on the Sell Reversal tab.

## Root cause (corrected from the card's own working hypothesis)

The card's evidence pointed at `v8_tc_score_ticks`. Tracing the actual render path: the bucket
tab's TC /100 column is **not** server-computed per basket at all — it is filled client-side, once
per page load, from `ctx.tcScores` (`GET /api/trade-check/position-stars-v2`, `tc_position_stars_v2`
table), the SAME map the master Open Positions table uses. That table:

- is **written** only for symbols currently `OPEN` in `v8_paper_positions` (`tc_position_stars_v2.py`
  `run_position_stars_v2()`), so a qualified name that has never been traded has no row at all →
  the page's `x.tc_score` stays `null` → `tcCell()` returns the bare `'--'`.
- is **read** with no recency bound (`DISTINCT ON (symbol, side) ORDER BY computed_at DESC` over
  *all* history), so a symbol that *was* open weeks ago keeps rendering its last-ever star forever,
  even after the position closed — TATAELXSI's 82 traces exactly to `best_any_score100=81.9` computed
  08-Sep (its last day in the book); HCLTECH's 56 to `55.5` computed 15-Sep. Neither number is today's.

Both symptoms are the same defect: the qualified table is candidate-scoped (every name that cleared
today's filters, traded or not), and its TC score was reading a position-scoped, non-recency-bounded
map instead of being computed for the candidate.

## What changed

| where | change |
|---|---|
| `tc_resolver.py` | `get_primary_best_card()` added — exposes `tc_v4_dual.best_card` (the cc#1033 locked score/max ratio, the SAME selection `tc_position_stars_v2`'s batch uses) through the resolver, so a caller needing the best-of-four pick imports nothing versioned (cc#738/cc#1549). |
| `v8_live_tc.py` (new) | `attach_live_tc(rows, basket)`: for every row, calls `tc_resolver.get_primary_styles()(symbol, "ALL")`, best-of-four via the resolver's own selector, and sets `tc_score` (score100), `tc_bucket` (label), `tc_band` (verdict10), `tc_opposes`, `tc_side_bucket`/`tc_side_score` (the tab's own side, for the hover), `tc_source='live'`, `tc_ts`. A resolve failure sets `tc_score=None` and an explicit `tc_error` (the reason) — never a bare null. Up to 6 scorer calls in parallel (`ThreadPoolExecutor`); one bad symbol never loses the table. A 60 s per-symbol cache means the four basket calls one page load makes score each symbol once, not four times. |
| `v8_endpoints.py` `qualified()` | After the existing F&O-ban chip pass, `v8_live_tc.attach_live_tc(rows, basket)` runs for **all four baskets** (one shared function, `basket` is a path param) and `tc_live` (resolved/failed counts + engine name) rides in the response alongside `stocks`. `v8_tc_score_ticks`, `tc_position_stars_v2`, `_tc_score100_for_basket` / `_registry_passcount`'s own `tc_score100` field (the separate FUNNEL capsule, cc#2099 — a different UI element, out of this card's scope) are all untouched. |
| `v8_dashboard.html` | `v8NormRow()`'s `ctx.tcScores` fallback is now **master-surface only** (`ctx.surface!=='basket'`) — a basket row's `tc_*` fields come straight off `qualified()`, live, and must never be silently overwritten by the stale position-stars-v2 map. `tcCell()`: `tc_score==null` with a `tc_error` present renders a small `err` badge (title = the reason) instead of falling through to the bare `'--'` a real "no signal" state uses. |

`node --check` on all 8 inline script blocks of `v8_dashboard.html`; `ast.parse` on all three Python
files; the resolver-import guard (`test_tc_resolver_guard.py`) passes — no versioned `tc_*` import
was added anywhere.

## Tests — `tests/test_v8_live_tc.py`, 7 cases, DB-free (a fake scorer stands in for `tc_v4_dual`), all pass

| test | what it proves |
|---|---|
| best-of-four by ratio + side fields | the resolver's own `best_card` (0.60/0.476/0.4545/**0.778**) picks the SELL-MOM card even on a BUY tab; `tc_opposes=True`; the tab's own-side card (BUY-REV, 60.0) rides the hover, never the headline |
| same result from a SELL tab | `tc_opposes=False`, side fields flip to the SELL-MOM card itself |
| error shapes | an engine error, no result, no card, and a card with no `score100` each produce a distinct, non-null `tc_error` string — never a silent `None` with no reason |
| `resolve_many` isolation + caching | a raising symbol (`BAD`) never loses `INFY`'s result; a second basket call reuses `INFY`'s cached raw result (no re-score) and retries `BAD` (failures are never cached) |
| `attach_live_tc` mutation + counts | rows get `tc_score`/`tc_error`/`tc_source='live'`/`tc_ts` in place; the summary's `resolved`/`failed` counts match; empty input is a no-op |
| `side_for` | `sell_*` baskets → SELL, everything else → BUY |

## What could not be verified from this sandbox

No `DATABASE_URL` is configured here, so `qualified()` cannot be exercised end-to-end against the
real DB (the resolver's scorer itself needs live `v8_metrics`/GVM/RSI/results-calendar reads well
beyond what a `run_sql`-only harness can replicate). The card's own verify section anticipated this
("Claude-web cannot browse scorr.in"). **Live checks for the founder/Fable:**
- `/v8` → Sell Reversal tab: INFY and KPITTECH (never in the book) now carry a `/100` and a bucket
  badge, or an `err` badge with a reason — never a bare `--`.
- The four in-book names (HCLTECH, LTM, MPHASIS, TATAELXSI) should now show **today's** live score,
  which will legitimately differ from the stale position-stars-v2 numbers quoted in the card's own
  evidence (81.9/55.5) — that is the fix working, not drift.
- DB spot-check: `SELECT * FROM cc_tasks WHERE id=2214` for the pushed sha, then re-run the resolver
  standalone per symbol against the live DB to confirm today's numbers.
