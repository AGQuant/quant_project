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

## Section D — same number, two formulas. CANDIDATES ONLY, NOT CONFIRMED.

Files defining what looks like the same metric. **The formulas have not been compared yet**, so none of
these is a confirmed clash and none should be actioned on this list alone:
- RSI: `invest_check_v2.py`, `tc_v4_endpoints.py`, `v12_backtest.py`
- Pivots: `invest_check_v2.py`, `trade_check_v34_endpoints.py`, `tc_v4_endpoints.py`, `v8_intra_backtest.py`,
  `buy_reversal_simulator.py`, `v8_paper.py`
- `fall_from_day_high` / `hourly_pct`: `deriv_metrics.py` (canonical) vs `v8_signal_writer.py`,
  `bt7_harness.py`, `v8_filter_killswitch.py`, and two served pages (`v8_dashboard.html`, `scorr_v13.html`)
- PCR: `pcr_endpoints.py`, `pcr_mood.py`

Precedent says this section is where the damage hides: cc#854 (weekly RSI 45 vs 40 killed a basket
silently) and cc#1872 (a whole second A/D implementation in `scorr_result_corner.html`).

## Section A — same question, different source. NOT DONE.

Honest status: **not started.** It needs the census read-set extended from one level of helper expansion
to full depth across 676 routes, then grouping by question. I did not do it and I am not going to imply
otherwise. It is the largest remaining piece of this card.

Out of scope by the card's own instruction: the TC family (cc#1982/1983, already ruled) and cc#1549
Ask Scorr v3.3 (founder parked).

## Method and limits

Static analysis: `ast` for routes, regex over source for writers, `scheduler_master` and job history from
the live DB for run timing. The route table was **not** dumped from a running app — the card asked for
that and this container cannot boot `main.py` safely. Registration order in `main.py` gives the same
answer for the one duplicate found, and that is stated rather than glossed. Two regex false positives
were found and corrected during the pass; the same risk applies to any candidate in Section D.
