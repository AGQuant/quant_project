# SPRINT 5 — SILENT BLANK SWEEP · cc#1095

Make "no data" impossible to mistake for "average", and stop the false alarms.

Six pushes. Four are done and proven; **two are open and say so here rather than being written up as
finished** — P2 needs its first run, P4 needs a measurement this container cannot take. Each section
below states the number it moved and the query that proves it.

| Push | SHA | State | What moved |
|---|---|---|---|
| P1 the guard | `b0b6640` | done | 22 scored metrics measurable, derived from PARAMS |
| P2 alert + registration | `6d863f0` | **registered, not yet live** | ops_log row pending first chained run |
| P3 source_coverage | `2d33c0c` · `33eec43` · `9951321` | done | 22 of 27 benchmark rows now carry coverage |
| P4 detail columns | — | **STOPPED at the gate** | no measurement possible here |
| P5 shrink check | `b170d88` | done | one false alarm at 1,742 symbols stops firing |
| P6 this file | — | done | — |

---

## P1 — the guard

`gvm_coverage_guard.py`. For every scored GVM metric, measure how much of the scored universe its
**resolved** source actually covers. Read-only; it writes nothing to `gvm_scores` and changes no
score.

The metric list is **derived** from `gvm_company_report.PARAMS` and `_M_EXTRA` at call time, never
copied. A hardcoded list would be correct on the day it was written and wrong the first time a row
is added to PARAMS — and that failure would be silent, which is the exact shape of defect this file
exists to end. Resolution mirrors the report: `_COMPUTED_COLS` sources are measured through their
SQL expression, `_NATIVE_FALLBACK` sources are measured primary-first then fallback, and
`fallback_used` records whether the fallback was doing the carrying.

**Why it exists:** cc#1094 was not one bad column, it was a class. `screener_raw."Operating profit
growth"` was populated for 0 of 1,816 rows, so OPM Expansion rendered a dash with rank `#-/0` on
every company page and Trackrecord quietly averaged 7 of 8 metrics platform-wide. A founder
screenshot found it, not the system. The cc#828 guard could never catch it: that one alerts when a
column *falls* from above 90%, and this class never falls — it starts at zero and stays there.

## P2 — the alert, and the registration

`gvm_coverage_guard.alert()` writes **one `ops_log` row per run** — `category='alert'`,
`title='GVM_METRIC_COVERAGE'` — carrying the under-50% list, the 50–90% warn band, the unreadable
list and the counts.

The row goes in **every** run, not only on a finding. A guard that writes only when it is unhappy is
indistinguishable from a guard that is not running.

**Chained, not given a wall-clock slot.** The card asks for "daily after the GVM recompute", and
`scheduler.py` already carries that reasoning for `screeners_eod`: a fixed time races the very job
it depends on and, on a night GVM ran late, would measure yesterday's universe while reporting
today's date. Chained into `_bg_gvm` after `screeners_eod`, it cannot measure a universe that has
not been rebuilt yet. `record_run` fires explicitly, because a chained job never passes through
`_spawn` and the registry would otherwise show "never run" for a job running nightly.

```sql
SELECT job_name, category, active, module, function
  FROM scheduler_master WHERE job_name = 'gvm_coverage_guard';
-- gvm_coverage_guard | chained | true | gvm_coverage_guard.py | alert
```

> **OPEN — ENGINE_LIVENESS_RULE 13829 is not satisfied and this file does not claim it is.**
> The registry row exists; the badge follows the data. The chain next fires after `gvm_recompute`
> at 01:30 IST. P2 closes when this returns a row and its counts are quoted:
> ```sql
> SELECT session_ts, details FROM ops_log
>  WHERE title = 'GVM_METRIC_COVERAGE' ORDER BY session_ts DESC LIMIT 1;
> -- 0 rows as of 18-Aug 21:20 IST
> ```
> **One thing worth knowing:** the `scheduler_master` INSERT landed with `active = false` despite
> `true` being passed. Caught on read-back and corrected with an UPDATE. A registry-derived
> enumeration would have skipped the job entirely — registered but invisible, which is the
> ENGINE_LIVENESS failure mode wearing a registration.

## P3 — source_coverage on every benchmark row

Every PARAMS row in `/api/gvm/company` now carries
`source_coverage {filled, universe, pct, fallback_used}`. A dashed row can say **why** it is dashed:
before this, "OPM Expansion —, rank #-/0" looked identical whether the company had nothing to report
or the column was empty for the entire universe.

Stamped in **one** place after `params_out` is assembled, so the two row builders cannot drift.
Cached by `gvm_scores`' own `MAX(score_date)` rather than a TTL — the map can never outlive the
universe it describes. Payload only: nothing feeds rating, rank, pillar arithmetic or the cc#828
part_3 exclusion rule.

**It took three pushes, and the middle one is the finding.** `_transform_param` in
`gvm_report_endpoints.py` is an explicit whitelist, so the new field was added at the source and
silently dropped one layer out — measured on the live payload as 27 rows carrying zero. A field
dropped in transit is the same defect this sprint is closing, which is why the payload was read
rather than the diff trusted.

**The first coverage table, scored universe 1,791:**

| metric | filled | pct | fallback carrying |
|---|---|---|---|
| div_yield | 1791 | 100.0% | |
| ret_1m · dma_50 · dma_200 · rsi_month · vol_trend | 1789 | 99.9% | |
| opm | 1787 | 99.8% | |
| qoq_sales | 1777 | 99.2% | |
| roce | 1772 | 98.9% | |
| inst_abs | 1767 | 98.7% | |
| int_cov | 1754 | 97.9% | |
| inst_chg | 1749 | 97.7% | |
| **opm_exp** | **1734** | **96.8%** | |
| sales_3y | 1712 | 95.6% | |
| pe | 1652 | 92.2% | |
| profit_3y | 1644 | 91.8% | |
| qoq_profit | 1606 | 89.7% | |
| sales_5y | 1605 | 89.6% | |
| ret_1y | 1576 | 88.0% | **YES** |
| ret_52w_idx | 1573 | 87.8% | **YES** |
| profit_5y | 1543 | 86.2% | |
| ret_3y | 1336 | 74.6% | **YES** |

**Nothing is under 50%.** Six metrics sit in the 50–90% warn band, and three of those are only that
high because the `universe_technicals` fallback is carrying them — `ret_3y` at 74.6% is the thinnest
metric on the platform and its primary column is thinner still. That is exactly what `fallback_used`
exists to show.

**And the one that gives this sprint its reason:** `opm_exp` reads 1,734 of 1,791 at 96.8%. That is
the cc#1094 column that was 0 of 1,816 a day earlier. The fix held, and now the system can see it
without a founder screenshot.

Five rows carry no coverage entry by design — `fwd_pe`, `hist_pe`, `sector_pe`, `pb`,
`annual_upside` are `_peer_block` Valuation rows built outside PARAMS, so there is no source to
measure and nothing is emitted rather than a fabricated number.

## P4 — detail columns universe-wide

> **STOPPED AT THE GATE, which is the gate working.**
>
> The card is explicit: measure 50 symbols first, and STOP if the projected full-universe runtime
> exceeds 10 minutes. That measurement has to run against the real database with real row counts.
> This container has no `DATABASE_URL` and no `psycopg`, so any number produced here would be a
> guess wearing a measurement label — which is the one thing a hard gate exists to prevent.
>
> Three routes were proposed in the room; none has been taken unilaterally, and the universe-wide
> persist step has **not** been written, because writing it would assume the gate clears.

## P5 — the shrink check compares complete days only

`scheduler._check_universe_shrink` took the two most recent `price_date` values whatever state they
were in. On 17-Aug it read **1,825 → 83** and raised a 1,742-symbol alarm (`cc_task_logs` 2724)
against a day that finished at 1,819. An alert that cries wolf at 1,742 is worse than no alert: the
next real one is read as noise.

```sql
SELECT price_date, COUNT(DISTINCT symbol) FROM raw_prices
 GROUP BY price_date ORDER BY price_date DESC LIMIT 3;
```

The gate is a floor on the newer day rather than an ingest-completion marker, because no such marker
exists in this schema. A day under half the previous day's coverage is recorded as **INCOMPLETE** —
visibly, in `ops_log` under `universe_shrink_incomplete` — and no shrink alert fires. Recorded
either way: an incomplete day is itself a finding.

Exercised against the real numbers from both logged alarms and the drop this check was built for:

```
 1825 ->   83   INCOMPLETE, no alert     cc_task_logs 2724, the false alarm
 1819 -> 1779   alert: 40 symbols        cc_task_logs 2725, a real 2.2% drop
 1819 -> 1825   quiet                    growth
 1791 -> 1780   alert: 11 symbols        just over the >10 threshold
 1791 -> 1785   quiet                    under the threshold
 1717 -> 1676   alert: 41 symbols        17-Jun, why this check exists
```

The false alarm stops; every genuine case it was built for still fires.

---

## What this sprint did not close

1. **P2's first run.** Registered is not live. The ops_log row lands on the 01:30 chain; the counts
   in the P3 table above are what it should contain, so the two can be checked against each other.
2. **P4's measurement.** Needs a timing run where the data is. Awaiting Fable's choice of route.
3. `ret_3y` at 74.6% with the fallback carrying it is the thinnest metric on the platform. Nothing
   in this sprint fixes it — the sprint's job was to make it *visible*, and it now is.
