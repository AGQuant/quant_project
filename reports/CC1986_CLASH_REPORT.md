# cc#1986 — Clashing endpoints, deep pass

**Read-only.** Nothing was changed. 10-Sep-2026, ~23:35 IST.
Scope: 677 route registrations across 257 Python files, plus the writer map for every table.

## What the founder needs to know, in order of harm

**1. The EOD engine overwrites the live 15:20 tick on `v8_metrics`. CONFIRMED. This is the one that costs money.**
Two jobs write the same row of `v8_metrics`, keyed `(symbol, score_date)`:
`v8_signal_writer.py:1011` every 5 minutes in market hours, and `v8_engine.py:306` once at EOD.
`bg_v8_eod` ran today at **15:50 IST — after** the writer's last tick at 15:20. Both use
`ON CONFLICT (symbol, score_date) DO UPDATE`, so the EOD run wins on every column it does not guard.
`v8_engine` guards only three: `mom_2d`, `day_1d` and `sector_day` are wrapped in `COALESCE(existing, new)`.
About eleven others are still bare overwrites: `gvm_score, dma_20, dma_50, dma_200, daily_rsi,
rsi_month, rsi_weekly, month_return, week_return, year_return, eod_chg`.
**This is not theoretical.** The comment at `v8_engine.py:331` records that this exact bug already ran
in production: the unguarded `sector_day` overwrite "wiped the live value back to null every single
trading day" until cc#1461 added the guard. The remaining eleven columns have the same shape and no guard.
It also sits against the V8 architecture lock in CLAUDE.md ("EOD frozen: gvm_score only; all 19 other
metrics live every 5 minutes"). Today the EOD pass writes far more than gvm_score.

**2. `GET /api/v8/metrics/all` is registered twice. CONFIRMED, no harm today, but it is a loaded gun.**
`v8_endpoints.py:1164` and `main.py:1991` both claim the path. FastAPI matches in registration order,
and `app.include_router(v8_router)` is at `main.py:816` — before the duplicate at line 1991.
So the **router version answers and the copy in `main.py` is unreachable dead code**.
The two are not the same payload: the live one joins segment, verdict, futures theme, live v21 metrics
and Vol R (RVOL); the dead one is a bare `SELECT` off `v8_metrics` with none of that.
If anyone ever moves that `include_router` line below 1991, the dashboard Master tab silently loses
segment, theme, verdict and Vol R. It is also logic living in `main.py`, which rule 4 forbids.
Already carded: **cc#1984** owns the fix. This pass confirms it and names the winner.

**3. Nothing else clashes on paths.** Across all 677 registrations this is the *only* duplicate
(method, path) pair. That is a clean result and worth stating plainly.

## Section C — same path, two handlers. COMPLETE.

Method: every `@router.<verb>` / `@app.<verb>` decorator parsed with `ast` across 257 files, each path
resolved against its own `APIRouter(prefix=...)` (29 routers carry one), then grouped by (method, path).
Winner determined by registration order in `main.py` (no `include_router` in this app passes a prefix,
so the decorator path plus the router prefix is the full path).

| Path | Handlers | Winner | Harm |
|---|---|---|---|
| `GET /api/v8/metrics/all` | `v8_endpoints.py:1164`, `main.py:1991` | `v8_endpoints.py` (mounted line 816, before 1991) | None today; dead copy is a trap. cc#1984 |

**Candidates rejected, and why.** A first pass flagged 11 duplicates including `/trades`, `/positions`,
`/summary`, `/status`, `/preview`, `/run`, `/tick`, `/backfill`, `/performance`, `/intraday`, `/health`.
All but one were **my own error**: I had not resolved `APIRouter(prefix=...)`. `/trades` is really
`/api/v10/trades`, `/api/v14/trades`, `/api/qsr/trades`, `/api/bt6/trades` — different books by design,
exactly the repeated tail cc#1979 already ruled is not a clash. Corrected before reporting.

## Section B — same table, two writers. CONFIRMED LIST COMPLETE, triage done.

42 tables have more than one writing file. Most are correct by design and are not clashes:
- **Append-only log / config / queue tables** — `ops_log` (51 writers), `session_log`, `cc_task_logs`,
  `app_config`, `job_runs`, `ops_metrics_t1_queue`. Many writers is the point; rows do not contend.
- **Coordinated split by time or scope** — `option_chain`, `pcr_daily`, `pcr_intraday`, `options_oi_daily`,
  `bt14_fut_oi`, `perf_request_log`: one writer inserts, `scheduler.py` only DELETEs on retention purge.
- **Replay vs live pairs** — `v8_paper_positions`, `v8_paper_trades`, `v8_paper_pivots`, `qb_nav_daily`,
  `quant_paper_positions`: a replay module writes the same tables as the live one. Not proven to collide,
  but this is the next place to look after item 1. Listed, not confirmed.

**Confirmed collision: `v8_metrics`** — see item 1 above. Also worth a look on the same pattern:
`v8_qualified` (`v8_engine.py` + `v8_signal_writer.py`, same two jobs, same EOD-after-live timing).

**Candidates rejected, and why** (both were false positives from regex, caught before reporting):
- `v8_funnel_counts` / `test_funnel_day_scope.py` — the file does not write. Its
  `re.findall(r"INSERT INTO v8_funnel_counts", wsrc)` **greps the writer's source** as a test assertion.
- `polished_news` / `news_guards.py` — the match is inside a docstring which states there is no Python
  insert path for `polished_news` at all. It confirms the CLAUDE.md rule rather than breaking it. The only
  real writer is `news_tagger.py`, and it only sets `mentioned_symbols` on already-written rows.

## Section D — same number, two formulas. ONE CONFIRMED, rest still candidates.

**CONFIRMED (labelling): the V13 filter page describes `fall_from_day_high` wrongly.**
`scorr_v13.html:238` tells the user the field is *"Fall off the **2-day** high %"* and names its source as
`v8_qualified.metrics (live)`. That field is written by `v8_signal_writer.py:929`, whose own comment is
explicit: *"(live - today high)/today high * 100 ... today high = fyers_eq day high"* — **today only, not
two days.** The stored values agree with today-only: sampled `v8_qualified.metrics` rows are -0.04%, -0.93%,
-0.12%, -0.08%, -0.02%, which are intraday pullbacks, not two-day falls.
A real 2-day version does exist, but in a different engine — `tc_v4_dual.py:255`, TC rule R10 ("recovery off
2-day low / fall from 2-day high"). The V13 page has borrowed R10's wording for a field that is not R10.
Harm: anyone filtering on this in V13 is filtering on a different quantity than the label promises.
Fix is a label, not a formula — but it must be checked against R10 so the two stay distinguishable.

**Duplicated formula, same maths, different SOURCE for the day high. Candidate, harm unquantified.**
`deriv_metrics.py:528` computes `(cmp_px - hi)/hi*100` where `hi = max(high) over today's stored intraday bars`.
`v8_signal_writer.py:929` computes `(live - day_high)/day_high*100` where `day_high = bar["high"]` from the
fyers_eq quote. The arithmetic is identical (deriv rounds to 2dp, the writer does not), but the two day-high
inputs are not the same object: an exchange quote high and a max over stored bars can differ when bars are
gapped or a tick is missed. Same named number, two sources — quantifying it needs a same-moment read of both
surfaces, which this pass did not do.

**Rejected — not copies at all** (same regex false-positive class caught twice already in Section B):
`v8_filter_killswitch.py` and `bt7_harness.py` only *mention* `fall_from_day_high` in docstrings about
policing and policy-skipping; neither computes it. The served pages **render**, they do not recompute:
`v8_dashboard.html:4786` is `${sign(x.fall_from_day_high)}%` straight from the payload, and `scorr_filters.html`
passes it through. So the cc#1872 risk (a page carrying its own reimplementation) does **not** apply here.

**Still candidates, formulas NOT compared:**
- RSI: `invest_check_v2.py`, `tc_v4_endpoints.py`, `v12_backtest.py`
- Pivots: `invest_check_v2.py`, `trade_check_v34_endpoints.py`, `tc_v4_endpoints.py`, `v8_intra_backtest.py`,
  `buy_reversal_simulator.py`, `v8_paper.py`
- PCR: `pcr_endpoints.py`, `pcr_mood.py`

Precedent says this section is where the damage hides: cc#854 (weekly RSI 45 vs 40 killed a basket
silently) and cc#1872 (a whole second A/D implementation in `scorr_result_corner.html`).

## Section A — same question, different source. FIRST PASS DONE, one CONFIRMED clash.

Method: every route's read-set resolved to full helper depth (transitive call walk, depth 6, across
modules), table names taken from SQL string literals and then **intersected with the 283 real tables
in `information_schema`** — an unfiltered pass matched English words out of prose (`FROM the`,
`INTO a`) and was discarded. 76 of 677 routes resolve to a non-empty real read-set; the rest either
run no SQL or reach it through a helper this walk cannot resolve, and are NOT claimed as clean.

**CONFIRMED: "what is the PCR?" has five answers today.** Same question, five sources, and they
disagree by more than a rounding step:

| source | NIFTY value | as-of |
|---|---|---|
| `pcr_intraday.pcr_atm5` | **1.938** | 15:25 |
| `oi_structure_daily.pcr` | 1.332 | 10:55 |
| `oi_structure_daily.pcr` | 1.303 | 15:20 |
| `pcr_daily.pcr` | 1.302 | today |
| `pcr_intraday.pcr_total` | 1.287 | 15:25 |
| live off `option_chain` (near expiry) | 1.30 | 15:35 |

Routes: `/api/daily/pcr` reads `pcr_daily`; `/api/pcr/intraday` reads `pcr_intraday`;
`/api/oi/structure` computes it live from `option_chain`; `/api/pcr/mood` reads `pcr_daily` **and**
`option_chain` together and bands the result.
The 1.938 is the ATM±5 band and is a legitimately different measure — but it is stored in a column
called `pcr` beside a whole-chain `pcr_total`, so a surface reading "the pcr column" gets a number
50% higher than the chain ratio with nothing in the name to say why. The other four are the *same*
measure kept by four independent writers, agreeing only to about the third decimal.
This is not a new suspicion: `mobile_home_derivatives.py`'s own docstring already states the Home
tape PCR (`pcr_mood.latest_pcr()`, banded) and the Home card PCR (`oi_structure()`'s chain ratio)
are different composers — two PCRs on one screen, by design, undocumented on the screen itself.

**Rejected, per the card's own rule.** The repeated `/trades`, `/positions`, `/performance`,
`/nav` tails across `/api/v10`, `/api/v14`, `/api/qsr`, `/api/qb` and `/api/v8` resolve to
disjoint per-book tables (`v10_trades`, `v14_trades`, `qsr_trades`, `qb_nav_daily`,
`v8_paper_trades`). Different books by design — cc#1979 already ruled this, and the read-sets
confirm it rather than contradict it. Not padded into the report.

**Open candidate, not confirmed.** `/api/v8/positions` resolves to `cmp_prices` + `personal_journal`,
while `/api/clients/positions` and `/api/test/positions` resolve to `v8_paper_positions`. Three
routes a reader would call "the V8 positions" over two different stores. It may be correct (journal
vs paper book are different products) but it is not self-evident from the paths, and rule 7's
context-isolation lock makes it worth a look.

## Method and limits

Static analysis: `ast` for routes, regex over source for writers, `scheduler_master` and job history from
the live DB for run timing. The route table was **not** dumped from a running app — the card asked for
that and this container cannot boot `main.py` safely. Registration order in `main.py` gives the same
answer for the one duplicate found, and that is stated rather than glossed. Two regex false positives
were found and corrected during the pass; the same risk applies to any candidate in Section D.
