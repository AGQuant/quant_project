# cc#2091 (P0 doctrine) — SCRAPE_UNIVERSE_TOP750_CANON_V1

Founder ruling, verbatim: *"now lets keep everything consistent which is remembrable quarterly
result scrape 750 by mcap, financial data history 750 by mcap, gvm history 750 by mcap rest all
cleanup."*

Built under explicit founder authorization (14-Sep ~19:44 IST, "fable not available, limit
finished and clear the queue push all to main, founder decision") — Fable is unavailable, the
founder is directing CC to build the remaining engine/backend queue directly for this reason.
Logged to `cc_task_logs` before acting, per rule 12's process.

## Item 2 (the open founder decision) — already answered, confirmed not guessed

This card's own item 2 framed the `sector_ops_metrics` UNION as an open question needing a
founder ruling ("Do NOT decide this unilaterally"). It was already answered: cc#2092's spec
carries `FOUNDER_RULINGS_14SEP_FINAL_READ_FIRST` → *"Clean top 750 by market cap, NO
sector_ops_metrics union (that open question is now closed — founder said 750)."* Verified this
is not a stray note but consistent with an independent, earlier founder ruling already in
`CLAUDE.md`: **ops-metrics is fully retired** (09-Aug-2026, session_log 18213) — the
`sector_ops_metrics` table the union pulled from has taken zero new writes since, confirmed by
reading `ops_metrics_pipeline.py`'s own retirement comments ("sector_ops_metrics and
sector_kpi_registry remain as a FROZEN ARCHIVE"). Dropping the union removes a special-case
inclusion for an already-retired subsystem, not an active dependency.

## Item 1 — the definition, locked in one place

`scrape_universe.py` (`in_scrape_universe`, `universe_symbols`, `UNIVERSE_CTE`) is the module
every scrape/retention caller already imports from (`result_corner.py`, `ops_metrics_pipeline.py`,
`fundamentals_scraper.py`, `scheduler.py` — confirmed by grep, no caller had its own duplicate
copy of this exact definition). All three forms dropped the `sector_ops_metrics` UNION — clean
top-750 by market cap, nothing else. Because every caller imports rather than re-derives, the
new definition propagates automatically; no caller-side changes were needed.

**Two genuinely separate, pre-existing duplicate copies found while checking for this** (not
part of the founder's ask, fixed because leaving a freshly-found duplicate in place after finding
it would be the exact drift this card exists to close):
- `scheduler.py`'s `_bg_ops_metrics_coverage()` docstring described the old union definition —
  comment-only fix; the function already imports `UNIVERSE_CTE` so its *behavior* already
  inherited the new definition automatically, only the words describing it were stale.
- `fy_end_backfill.py`'s own `_universe()` hand-rolled a **second, independently-drifted** copy
  of the ranking query — and it was wrong even under the OLD rules: `LIMIT 500`, never updated
  when cc#814 raised the cutoff to 750 back on 02-Aug. Replaced with a one-line delegate to
  `scrape_universe.universe_symbols()` — can never drift again. This script is a one-time,
  already-largely-run FY-end backfill (cc#703); low current risk either way, fixed because it's
  the same class of bug this card is unifying against.

## Item 4 — retention guard, read-only by design

Added `universe_retention_report(cur)` to `scrape_universe.py`: reuses `universe_symbols()` (the
one definition, never re-derived) to report universe size, `fundamentals_history`/`gvm_history`
distinct-symbol counts, how many of those sit outside the current universe, and which
current-universe symbols have zero `fundamentals_history` rows (under-coverage — the one number
actually worth watching).

**Retention is one-way, stated explicitly in the module docstring**: the universe gate controls
what gets newly scraped/computed going forward (this happens automatically — a symbol dropping
out of the live top-750 simply stops being enqueued, since the ranking re-evaluates live on every
call). It does **not** mean deleting that symbol's existing history — that would destroy the
point-in-time record a backtest needs and reintroduce survivorship bias by another door. The
module docstring says this in plain words and flags today's cleanup as a one-time, founder-
authorized exception, not a pattern for any future job to repeat. No new scheduled job was
built for this — a documented, reusable, read-only report function is the right-sized deliverable
for "guard against a future mistake"; wiring it into a recurring health page is a natural,
separate follow-up if wanted, not invented here.

## Item 3 — cleanup, the delete plan computed and shown before running

Real counts, computed and matched against this card's own stated numbers before touching
anything: **112 symbols / 7,157 rows** outside the clean 750 in `fundamentals_history` (card
said 112 — exact match); **1,110 symbols / 833,632 rows** outside the clean 750 in `gvm_history`
(card said the same numbers — exact match). Additionally, per cc#2092's own `do_not_touch`
assignment ("backfill_step_partial rows — cc#2091 owns their deletion"), all `gvm_history` rows
carrying the old frozen-value method tag were included in scope regardless of universe
membership — checked first: those rows' `method` breakdown showed exactly two values,
`backfill_step_partial` (1,400,194 rows / 1,227 symbols — the condemned frozen ones) and `NULL`
(190,337 rows / 1,847 symbols, spanning 2026-05-30 to 2026-09-13 — the ongoing, legitimate
nightly GVM process, confirmed by date range, left untouched). Combined delete plan: **1,512,078
rows / 1,739 symbols** — computed via one query with the exact `OR` condition before running,
then executed, and the **actual DELETE rowcounts matched the pre-computed plan exactly**:
7,157 and 1,512,078.

**Post-cleanup, verified for real**: universe 750 / `fundamentals_history` 738 distinct symbols
(0 outside universe) / `gvm_history` 737 distinct symbols (0 outside universe, and the entire
1.4M-row `backfill_step_partial` set is gone — confirmed by re-running the method breakdown:
only `NULL` remains, 78,453 rows). 12 top-750 symbols currently have zero `fundamentals_history`
rows — a real, small, honest scraping-backlog gap, not a universe-definition problem; named here
rather than hidden, not something this card fixes.

## do_not_touch respected

`gvm_scores` (current snapshot) — untouched, zero writes. `raw_prices`/`intraday_prices` —
untouched. No row was deleted before computing and matching the plan against this card's own
stated counts first.

## Verify

`py_compile` clean on all three changed files. Real production Postgres throughout — every
number in this report is a live query result, not a projection: the pre-delete counts matched
the card's own stated numbers exactly, and the actual DELETE rowcounts matched the pre-computed
plan exactly, with zero surprises. Post-cleanup the three universe numbers line up (750/738/737,
differing only by the named, small, honest under-coverage gap) and the frozen `backfill_step_partial`
rows are fully gone from `gvm_history`.

**Sequencing note for the next card**: cc#2092 (GVM point-in-time backfill) explicitly depends on
this card finishing first — its own spec says "Do not start this card before cc#2091 has cleaned
the table, or the rebuild will process rows that are about to be deleted." This card is now done;
cc#2092 is unblocked and is next in the queue.
