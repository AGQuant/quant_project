# cc#2125 — mcap_rank frozen since June: diagnostic re-verification (P1 DATA, NO WRITES)

**GATE ACKNOWLEDGED: this card requires founder GO before the first write. This pass is
read-only. Zero writes were made — every query below is a SELECT. No table was created, no row
was updated.**

## The spec's own drift numbers, independently reproduced (not copied)
Every headline figure in the card's spec was recomputed from scratch this pass, with fresh SQL
against live `screener_raw`/`input_raw`, not copied from the spec text. All match exactly:

| Claim | Spec | Independently recomputed |
|---|---|---|
| Symbols matched (nse_code) | 1,725 | **1,725** |
| Companies in the wrong cap band | 103 | **103** |
| Average rank move | 67.1 places | **67.1** |
| Maximum rank move | 778 | **778** |
| Enter top 500 | 14 | **14** |
| Leave top 500 | 23 | **23** |
| Band migration (small→micro / micro→small / mid→small / large→mid / small→mid / mid→large) | 51/27/9/6/6/4 | **51/27/9/6/6/4** |
| All 12 named movers (VEDL, TATACONSUM, INDUSTOWER, LODHA, LENSKART, PATANJALI, PIIND, IREDA, HUDCO, GODFRYPHLP, LTTS, UBL) | stored/actual ranks as listed | **exact match, every rank** |

Method: `RANK() OVER (ORDER BY market_cap DESC)` computed fresh from `screener_raw` (1,881 rows,
one batch, `loaded_at = 2026-09-08 06:37:10`, zero NULL market_cap, zero duplicate `nse_code` —
checked), joined to `input_raw.mcap_rank` (2,008 rows, `loaded_at` max `2026-07-18 12:59:33`, two
months stale) on `nse_code`. Confirms the founder's own briefing is accurate to the row.

## BAGMANE — resolved more precisely than the original three-way question
The spec asked: delisted, renamed, or missed by the reload? None of the three, precisely stated:
**BAGMANE is present in both tables today** — `screener_raw` (fresh, 2026-09-08, market_cap
35,441.6) and `input_raw` (id=1983, market_cap 35,441.6, `gvm_segment='REITs'`) — it was never
lost. What's null is specifically `input_raw.mcap_rank`/`cap_category` for this one row.

Checked whether REITs are categorically excluded from ranking (a plausible, more benign
explanation) — **no**: 7 of 9 REIT-segment rows in `input_raw` carry a normal rank (BIRET 305,
EMBASSY 217, KRT 197, MINDSPACE 267, NDRINVIT 769, NXST 323). Only BAGMANE and NXT-INFRA are
null. Widening the check: **250 of 2,008 `input_raw` rows have a null `mcap_rank`** — 205 of
those also have no `market_cap` at all (unrankable by definition, not a bug). The remaining
**45 rows have a market_cap but no rank** — BAGMANE among them. That set is dominated by newer
listings: the 2026 Vedanta group demerger spinoffs (VAML "Vedanta Aluminium Metal", VEDPOWER,
VOGL, VISL) and a long tail of small/micro names. **Conclusion: BAGMANE was added to `input_raw`'s
descriptive coverage after the last `mcap_rank` computation ran, and simply never received a rank
— a "new to coverage, never ranked" gap, not a delisting, rename, or reload failure.** This
independently corroborates the freeze finding (new rows keep arriving; the rank computation that
would cover them stopped running months ago).

## scheduler_master — the "does not exist" branch, confirmed
Searched `scheduler_master` for any job touching `mcap`, `cap_rank`, `screener`, or `nifty500`:
three matches, all `bg_tc_screener_*` (an unrelated subsystem — the TC Scanner's own screener
cache, `tc_screener_v2`/`tc_screener_cache`, nothing to do with `input_raw.mcap_rank`). **No
mcap_rank recompute job exists in scheduler_master at all.** Also confirmed there is no scheduled
job for the `screener_raw` reload itself — consistent with the founder's own words ("Screener CSV
I upload weekly basis"): it is a manual admin action, not a cron job. The real hook point for a
future recompute is `admin_data.py:85`, `async def load_screener(...)`, serving
`POST /api/admin/load_screener_from_drive` — confirmed by reading the file, not edited.

## Correction to the spec's own "why this matters" — read before deciding GO
The spec frames the risk as *"A Large Cap basket is currently capable of buying a mid cap and
refusing a large one,"* citing LODHA/LENSKART's presence in `quant_rebalance_log id=411`'s
large_cap candidate list. **Traced this to the actual selection code — the framing needs
correcting:**

`qb_composite_select.py` (large_cap **and** mid_cap selection, the module that produced id=411)
does **not** read `input_raw.mcap_rank` at all. Its own SQL (lines 70–73) computes the universe
rank fresh, every run:
```sql
WITH mcap AS (
    SELECT nse_code AS symbol, ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mrank
    FROM screener_raw WHERE nse_code IS NOT NULL AND market_cap IS NOT NULL
)
```
`qb_smallcap_select.py` does the identical thing for small_cap (its own line 103, same pattern).
`qb_alpha_select.py` (Alpha Multicap) references neither table — it has no cap-band gate to be
stale. **All three real basket-selection engines are already immune to this bug** — they never
depended on the frozen `input_raw` column, which is why id=411's own embedded `mcap_rank` values
for LODHA/LENSKART (84/86) are close to today's fresh numbers (86/87 — the small residual gap is
`ROW_NUMBER` vs `RANK` tie-breaking plus `screener_raw` having been reloaded once between the
2026-09-07 rebalance and today, not a bug). LODHA and LENSKART are in the large_cap candidate list
**because the real engine correctly placed them there** — the "mid" label was only ever true in
the stale `input_raw.cap_category`, which the trading engine never reads.

**The real blast radius, mapped by reading every call site, not assumed:**
| File | How it uses the stale field | Stakes |
|---|---|---|
| `qb_universe_builder.py` (cc#2123, shipped today) | 4 cap-band POOLS (`cap_large/mid/small/micro`) gate directly on `input_raw.cap_category` | Pool membership and the counts shown (100/149/744/732) are stale by the same 103-company drift. Follow-up flag below. |
| `native_router.py` (2 sites) | `i.cap_category = '{cap}'` — an app-facing cap-band filter | Real: a user filtering the native app by cap band gets the stale classification |
| `v12_endpoints.py` | `i.mcap_rank` sortable/filterable column | Real, but this file is `do_not_touch`-protected (cc#2123's own boundary) — noted, not touched |
| `qb_app_mobile.py` | `cap_category` looked up for display per symbol | Display-only, app QB pages |
| `hr_report.py` | `WHERE i.cap_category = 'large'` in one aggregate (median PE by band) | Internal report, not a live/founder-facing trading surface |
| `worker/fyers_feed.py` (Hard Line file — not touched) | Orders the live-feed **staged-subscription rollout** by `input_raw.mcap_rank` ("stage 1 = +500 largest"), with its own documented fallback when the rank is missing | Affects which symbols get a live feed **first**, not price, P&L, or any trading decision |
| `gvm_market_endpoints.py` | `i.cap_category, i.mcap_rank` in a display SELECT | Display-only, company detail |

No basket-selection engine buys or sells on the stale field. The real, live consequence is display
and filtering surfaces — most immediately **cc#2123's own new pools, shipped today**, which this
diagnostic obsoletes the freshness of within hours of shipping. Flagging that honestly rather than
letting it sit quietly: cc#2123's report already disclosed the Nifty 500 pool as stale; it did not
know at the time that the 4 cap-band pools share the same underlying staleness, because that fact
had not yet been traced. It is traced now, in this card.

## What was proposed, not run (per the GATE)
Per the card's own revised scope: an EOD-cadence rank recompute hooked into
`admin_data.py`'s `load_screener()` (no new schedule), writing a **NEW, per-date table**
(`CREATE TABLE`, no `ALTER` on `input_raw`) keyed `(symbol, rank_date)`, keeping session_log 85's
band edges exactly (large 1–100 / mid 101–250 / small 251–1000 / micro 1001+), backfilling the one
honest historical point (`screener_raw`'s single `loaded_at`, 2026-09-08 — no fabricated
intermediate dates), and retiring `nifty500_universe` in favour of deriving the top-500 pool from
the new per-date rank once it exists (not dropped, just superseded). None of this was implemented
this pass — no file was created or edited, no table exists yet.

## Verify (per the card's own checklist)
- Drift report names actual counts, not percentages alone — done, table above, every number
  independently recomputed.
- `scheduler_master` quoted directly — done, no matching job exists (the finding itself).
- BAGMANE has a stated cause — done, more precisely than the original three-way question.
- **Nothing was written — confirmed: every query this pass was a SELECT (schema introspection,
  `RANK()`/`ROW_NUMBER()` comparisons, `scheduler_master` lookups, code reads). No `CREATE`, no
  `INSERT`, no `UPDATE`, anywhere, on this card.**

## Stopping here for founder GO
This card's own gate: *"Post the drift table to cc_task_logs and STOP. Do not run the first write
until the founder replies GO."* Posted to `cc_task_logs` (task 2125) alongside this report.
`cc_tasks.status` set to `blocked` — not `done`, nothing shipped by design, no `commit_sha` for a
write that did not happen.
