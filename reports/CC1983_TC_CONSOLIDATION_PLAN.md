# cc#1983 — TC family consolidation: the plan

**PLAN ONLY.** Nothing is migrated, no writer is deactivated, no table is dropped by this card.
All four gates hold. 10-Sep-2026, ~23:45 IST. Canonical store: `tc_universe_ticks`.

## For the founder, in plain words

Seven tables have answered one question — *how good is this TC setup*. One of them is now canonical
and it is the cheapest of the lot. Your ruling of 22:25 ("V2 everywhere") settles *whether*; this
plan is *how*, *in what order*, and *what changes on your screen*.

**The saving is not the point, and I am not going to dress it up.** Deactivating every non-canonical
TC writer reclaims about **69 seconds of compute per tick cycle** and roughly **7 MB** of stored
rows. On Railway that is close to nothing. The real reason to do this is that four live columns
answer the same question from different scorers, and two canons already say "do not read these"
while the jobs stay wired. That is what costs — every future card has to work out which number is
real first. That is a good enough reason on its own.

**The one thing I would not do** is force `tc_position_stars_v2` onto the canonical store. It holds
a shape `tc_universe_ticks` genuinely does not have (see below). Migrating it would lose information.

## The honest numbers (scope item 6), read live from the DB 10-Sep

| job | active | last duration | table | rows | size |
|---|---|---|---|---|---|
| `bg_tc_universe_tick` | **yes — CANONICAL** | **154 ms** | `tc_universe_ticks` | 115,920 | 21 MB |
| `bg_tc_score_tick` | yes | 9,217 ms | `v8_tc_score_ticks` | 8,973 | 2,120 kB |
| `bg_tc_position_stars_v2` | yes | 8,798 ms | `tc_position_stars_v2` | 2,047 | 448 kB |
| `bg_tc_scanner` | yes | 11,253 ms | (decides entries; see S5) | — | — |
| `bg_tc_sim_tick` | yes | 47,331 ms | outcome sim | — | — |
| `bg_tc_position_stars` | no | — | `tc_position_stars` (v1) | 4,184 | 584 kB |
| `bg_tc_screener_v2` | no | — | `tc_screener_v2` | 14,920 | 3,184 kB |
| `bg_tc_score_daily` | never ran | — | `v8_tc_score_daily` | 0 | 32 kB |
| — (dead since 18-Jun) | — | — | `tc_cache` | 416 | 784 kB |

Canonical does the **full universe on all four buckets in 154 ms**. `bg_tc_score_tick` takes
**60× longer** to cover ~36 book symbols. That ratio, not the MB, is the argument.

`tc_screener_cache` is deliberately absent from this plan — it is cc#1982's and this card must not
pre-empt that ruling.

## Per store: consumers confirmed in code, and what migration costs

### 1. `v8_tc_score_ticks` — MIGRATE, number changes on screen
- Writer: `v8_pivot_star.py:212` via `bg_tc_score_tick` (active, 9,217 ms), book-scoped (~36 symbols).
- Consumer: `GET /api/v8/tc_score_latest` → the TC% column in `v8_dashboard.html`.
- Can canonical answer it? **Yes, and better.** `tc_universe_ticks` already covers every one of those
  symbols (full universe) at the same 5-minute beat, from the same `score_card`.
- What changes: the column is already V2 rules (cc#1548 repointed it), so this is a **source** swap,
  not a scorer swap. Expect the same number or a near-identical one, differing only where the two
  jobs ticked at slightly different seconds. **GATE 4: low risk, but list it.**
- Note for the record: `v10_endpoints.py:1023` already names this table FORBIDDEN as a source, and
  its job is still active. That is the gap this card closes.

### 2. `tc_position_stars_v2` — DO NOT MIGRATE, keep the writer, written reason
- Writer: `bg_tc_position_stars_v2` (active, 8,798 ms), gridded at `m==30`, 09:00–15:00.
- Consumers: `/api/trade-check/position-stars-v2` → `mobile/v8.html`, `trade_alerts_web.html`,
  `v8_dashboard.html`; `/position-stars/history` → `v8_dashboard.html`. Read path at
  `tc_v4_endpoints.py:702-727`.
- Can canonical answer it? **No, not without losing something.** This store holds a *star state per
  position over time* — a history shape keyed to open positions. `tc_universe_ticks` stores the
  aggregate score per symbol/bucket per tick and has no position dimension at all. A `/history`
  endpoint cannot be served from it.
- Proposal: **keep this writer**, and instead of migrating it, re-source its *input* so the star is
  computed from the canonical score rather than a second scoring pass. That removes the duplicate
  *computation* without losing the position history. Additive, reversible, and it is its own card.

### 3. `tc_position_stars` (v1) — RETIRE THE READ, writer already off
- Writer: `bg_tc_position_stars` — **already inactive** (last ran 08-Sep).
- Consumer: `/api/trade-check/position-stars` → `v8_dashboard.html`. **Still live-consumed off a
  table nobody writes.** That is the worst state of the three: a dashboard column being served from
  a frozen table with no staleness disclosure found on the read path.
- Migration: point that consumer at the v2 endpoint (same question, live data), then the table is
  inert. **Cheapest and highest-value move on this card — do it first.**

### 4. `tc_screener_v2` — RETIRE, no page consumer found
- Writer: `bg_tc_screener_v2` — already inactive. Table frozen at 14,920 rows.
- Consumers: `POST /api/admin/run-tc-screener-v2` and `GET /api/trade-check/screen-v2`
  (`tc_screener_v2.py:174`), both wired in `main.py:958`. **No `.html` file references it** —
  repo-wide grep, 10-Sep. It was the bridge scorer 41629 compared against before trusting V2.
- Migration: none needed. Retire the two routes the way cc#1982 retired `screen-cached` — keep them
  wired, return a plain "retired, see tc_universe_ticks". Table stays.

### 5. `tc_cache` — ALREADY DEAD, one live read left
- No active writer since 18-Jun-2026. 416 rows.
- Still read by `trade_check_v34_endpoints.py:160-168` (`/api/intraday/dashboard` funnel counts) and
  referenced by `previews/check.html` (a dummy-data preview, not a live surface).
- The funnel counts on that dashboard are therefore computed off a table that stopped being written
  in June. Migration: recompute those counts from `tc_universe_ticks`, or state the dashboard's
  as-of. Either way it is small and it is a correctness fix, not a saving.

## Ordered plan (scope item 4) — cheapest and least visible first

| # | move | verify | rollback |
|---|---|---|---|
| 1 | `tc_position_stars` v1 consumer → v2 endpoint | dashboard column shows today's date, not 08-Sep | revert one route reference |
| 2 | `tc_screener_v2` routes → retired response | both routes answer "retired"; no page breaks (no consumer exists) | restore two function bodies |
| 3 | `tc_cache` funnel counts → canonical or state as-of | funnel numbers move off June data | revert one query |
| 4 | `v8_tc_score_ticks` → `tc_universe_ticks` for the TC% column | same symbol/same tick: dashboard TC% == `score100` from canonical | repoint one query; writer untouched until verified |
| 5 | only then: deactivate `bg_tc_score_tick` | one row in `scheduler_master`, reversible | set `active=true` |
| 6 | separate card: re-source the star input from canonical | star values unchanged vs a day of v2 history | keep the second scoring pass |

Nothing is dropped at any step. Retirement = writer `active=false` + table kept. Earliest sensible
review date for actually removing any of these tables: **90 days after its writer goes inactive**,
matching the retention window the canonical store already uses.

## The three post-15:20 computes carried from cc#1910

The test the card sets is: does it SCORE, or does it MARK an outcome against a score already stored?

- **S3 `bg_tc_position_stars_v2` @15:30** — it SCORES (it derives a star from a fresh scoring pass).
  Under TC_CANON_V2_FINAL the last score tick is 15:20, so this must either stop at 15:20 or read
  the 15:20 tick from canonical. **Tighten it.**
- **S5 `bg_tc_scanner` @15:25 and 15:30** — it SCORES the universe to decide entries, riding
  `_is_market_hours` 09:15–15:30. Same verdict: its scoring must not run past 15:20. **Tighten it**,
  but only the scoring half — if any part of it closes or marks entries, that part stays.
- **S6 `bg_tc_sim_tick` @15:30** — this MARKS. It is the outcome sim's exit tick against scores
  already stored (`entry_tick_times`). A 15:30 run is legitimate. **Leave it alone.** The card warns
  explicitly not to tighten a window that is marking exits, and this is that window.

## One thing I could not resolve, flagged not chased

`bg_tc_lite` is **active** (199 ms, ran today), and `mobile_endpoints.py`'s cc#888 comment states it
writes `tc_screener_cache` "EVERY session". But that table's newest `run_date` is 08-Sep. Either the
comment names the wrong table (it may write `tc_cache`) or the job is no longer writing what it
claims. I did not chase it because `tc_screener_cache` freshness is cc#1982's and this card must not
pre-empt it — but whoever picks up cc#1982's ruling should settle it.

## Gates

No writer deactivated, no consumer migrated, no table dropped, no structure touched. Every
number-changing migration is listed above rather than buried: items 1, 3 and 4 change what is on a
screen, and each needs the founder's word before its execution card is written.
