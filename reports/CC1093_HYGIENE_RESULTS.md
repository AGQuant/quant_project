# cc#1093 — HYGIENE SPRINT

Five small fixes from the 17/18-Aug findings, each too small to be its own card. Four pushed; one
stopped on a decision that is not mine to take.

| Push | SHA | State | What moved |
|---|---|---|---|
| P1 v8_eod trading-day gate | `95ef9a2` | done, verify pending a Monday | dispatch gated; 16 other jobs listed |
| P2 lateness reporter | `50ad4a3` | done | one false LATE flag stops |
| P3 gvm_scores.market_cap | — | **STOPPED, diagnosed** | write path named; 1,791 of 1,791 rows explained |
| P4 ladder mcap binding | `f0879fd` | done and verified live | header and ladder agree on 5 of 5 symbols |
| P5 this file | — | done | — |

---

## P1 — bg_v8_eod gains a trading-day gate

**The finding is which half was wrong.** The registry read `h == 15 and m == 45` with no weekday or
holiday condition — and the dispatch matched it exactly:

```python
if h == 15 and m == 45: _spawn(_bg_v8_eod)
```

So the registry was **accurate** and the **code** was the defect. `expected_last_run` legitimately
resolved to a Saturday, and the sentinel read v8_eod as late every Monday (session_log 24585).
Fixing only the registration would have made the registry lie in the other direction.

Now gated exactly as `_bg_tc_scanner_eod` is, four lines above it in the same block:

```python
if now.weekday() < 5 and _is_trading_day(now.date()) and h == 15 and m == 45:
    _spawn(_bg_v8_eod)
```

Two EOD jobs either side of the close should not disagree about what a trading day is.
`_is_trading_day` is `nse_holidays`-backed with a weekend-only fallback, so a holiday resolves
through the same source the rest of the app uses. The job's own `_eod_ran_today` guard is untouched
— this narrows *when* it is offered a run, never whether it can run twice. `cadence_human` updated
to match, with the reason in `notes`: registry and code now say the same thing, which is the only
state in which either can be trusted.

**The list the card asks for.** 33 active `scheduler_loop` jobs register an exact `h ==`/`m ==` slot
with no weekday or trading-day condition:

```sql
SELECT job_name, cadence_human FROM scheduler_master
 WHERE active AND category = 'scheduler_loop'
   AND cadence_human LIKE '%h ==%'
   AND cadence_human NOT LIKE '%weekday%'
   AND cadence_human NOT LIKE '%_is_trading_day%';
```

Most are legitimately every-day work — log retention, news cleanup, news fetch, MF nightlies, GVM
and its backfills, lot sync. **The market-dependent subset that would read late the same way** is
sixteen: `bg_adr_pcr` 15:50, `bg_adr_pcr_retry` 16:00, `bg_tc_screener_precompute` 16:00,
`bg_stock_news_watchdog` 16:00, `bg_guardian_eod_oi` 16:15, `bg_feed_daily_log` 16:15, `bg_qb_eod`
01:15, `bg_pivots` 01:45, `bg_universe_pivots` 01:47, `check_pivots_health` 01:55,
`bg_v8_paper_exit_eod` 02:00, `bg_universe_technicals` 02:05, `bg_rvol_profiles` 02:10,
`bg_ca_daily_note` 09:00, `bg_master_watchdog_note` 09:05, `premarket_writer_check` 09:10.

Listed, **not swept**. Each needs its own judgement about whether a holiday run is harmful or merely
pointless, and sixteen gates added on my own judgement is a sprint wearing a P1 label.

> **Verify open.** The card's gate is "P1 closes on Monday 18-Aug reading NOT-late for v8_eod". Two
> things about that: 18-Aug-2026 is a **Tuesday**, and the fix landed at 21:20 IST, long after the
> 15:45 slot. Today cannot be the proof whatever weekday it is. The first honest reading is Monday
> 24-Aug, or the first trading day after a holiday — the case where a Saturday-resolved expected
> slot previously showed late and now should not.

## P2 — a job inside its own due minute is not late

Protocol One ran at 15:40:00.4 and flagged `bg_heal_intraday`, whose slot is 15:40. `last_due` had
returned today 15:40 four tenths of a second earlier, so the job was being judged for missing a slot
it had not yet been offered. It was due **now**, not overdue.

The existing 10-minute `grace` could never cover this — it does the opposite job, forgiving a run
that *finished* slightly before its due time. This is the other end of the same window, and it is
the card's wording exactly: grace is its own due minute.

Deliberately the minute and not more. A wider window would also hide a job that genuinely missed its
slot for nine of them, and that is a judgement about how long a slot may quietly slip.

```
bg_heal_intraday, the reported case            LATE -> ok
same job one minute later, genuinely missed    LATE -> LATE
job that ran on time today                     ok   -> ok
job that never ran, slot long past             LATE -> LATE
job finished 3 min before its slot             ok   -> ok
```

The false flag stops; nothing that was correctly late becomes quiet.

## P3 — gvm_scores.market_cap

> **DIAGNOSED, NOT CHANGED — and the diagnosis changes what the fix means.**

**The write path, named.** `gvm_nightly.gvm_recompute` builds `latest_rows` with
`mcap = row.get("market_cap")` from the merged dataframe, and that dataframe takes `market_cap` from
**`input_raw`** — `SELECT nse_code, company_name, market_cap, gvm_segment, fy27_growth FROM
input_raw`. It then DELETEs `gvm_scores` and re-inserts. `screener_raw` is never consulted for this
column.

**It is not staleness. It is a different source table**, and the numbers leave no room:

```sql
WITH j AS (
  SELECT g.market_cap AS gv, s.market_cap AS sc, i.market_cap AS ir
    FROM gvm_scores g
    LEFT JOIN screener_raw s ON s.nse_code = g.symbol
    LEFT JOIN input_raw   i ON i.nse_code = g.symbol
   WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores))
SELECT COUNT(*),
       COUNT(*) FILTER (WHERE gv IS NOT DISTINCT FROM ir) AS equals_input_raw,
       COUNT(*) FILTER (WHERE gv IS NOT DISTINCT FROM sc) AS equals_screener,
       ROUND(AVG(ABS(gv-sc)/NULLIF(sc,0)*100)::numeric, 2) AS avg_abs_pct
  FROM j;
-- 1791 | 1791 | 0 | 12.48
```

Every scored row equals `input_raw`. **Zero** equal `screener_raw`. 1,282 differ by more than 5%;
the average absolute difference is 12.48% and the maximum is 160%. BHARATSE — the founder's example
— is one of the 1,282: gvm 1,156.78 = input_raw 1,156.78, against screener_raw 1,557.75.

**Why the writer was not repointed.** That column is not display-only. `gvm_nightly` recomputes
`sector_ratings` from the fresh `gvm_scores` using **mcap-weighted GVM** —
`(grp[col] * grp["market_cap"]).sum() / total_mcap` — and `sync_gvm_cache` rebuilds `gvm_cache` and
`peer_averages` in the same transaction. Repointing the source would move the weight behind all 130
sector ratings on the next nightly run, and with a 12.48% average shift in the weights that is not a
no-op. The founder reads sector ratings on `/m/sector` and on the GVM card.

That is a decision about which table is the truth for a **weight**, not a bug fix. Three options are
in the room with the numbers attached; none has been taken unilaterally.

## P4 — the ladder's market cap

`gvm_page_extras` step 9 read `market_cap` from `gvm_scores`; it now reads it from `screener_raw`
via a LEFT JOIN — the same table the header has used since cc#450. The live GVM card was printing
two different market caps for the same company on one screen.

**Binding only**, and the restraint is the point: `gvm_scores.market_cap` is untouched, because it
is also the sector-rating weight. This fixes what is *displayed* and moves no rating.

**Verified live**, payload generated 18-Aug 21:23:05 IST:

| symbol | ladder before | ladder now | screener_raw |
|---|---|---|---|
| MINDACORP | 15,065 | 17,215 | 17,214.91 |
| BHARATSE | 1,157 | 1,558 | 1,557.75 |
| MUNJALAU | 869 | 1,205 | 1,205.3 |
| TALBROAUTO | 2,124 | 2,590 | 2,589.5 |
| SSWL | 3,292 | 4,921 | 4,920.82 |

Header 17,214.91 against its own ladder row 17,215 — the whole-rupee gap is the existing
`_r(mcap, 0)` rounding on ladder rows, not a source difference.

---

## Open

1. **P1's verify** needs a Monday (or the first trading day after a holiday). 24-Aug is the first.
2. **P3's ruling** — which table is the truth for the sector-rating weight. Until that is answered,
   the full-universe `gvm_scores` vs `screener_raw` parity check in the card's P5 is not a check:
   the two differ by construction on every row.
