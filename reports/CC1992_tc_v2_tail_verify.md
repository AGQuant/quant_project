# cc#1992 — TC V2 tail: verified already-done, one stale comment corrected

All three routes in this card's scope were already migrated/retired in a prior session, under
cc#1982's own spec. This card re-verifies that state with fresh evidence and closes item 4
(the stale-comment sweep), which had one live hit.

## Item 1 — every remaining reader of `tc_screener_cache` (grep, boundary-aware)

| File | What it is |
|---|---|
| `trade_check_v34_endpoints.py` | The retired writer (`run_tc_screener_precompute`, DDL + INSERT), kept per `do_not_touch` — the table itself is cc#1983's to review, not deleted here. `screen-cached` and `run-tc-screener` routes now return retired bodies (below). |
| `tc_screener_v2.py` | Module-level comment describing the V2 rebuild's own history — `tc_screener_cache` is named as the archive it compared against, correctly in the past tense. |
| `mobile_endpoints.py` | The retired `/api/mobile/check` handler (cc#1984) and one corrected data-source comment (cc#1984) — both already fixed. |
| `v10_endpoints.py` | One line naming `tc_screener_cache` as a **forbidden source**, by ruling — correctly discourages new reads, not a live read itself. |
| `deriv_metrics.py` | One comment line, past-tense, describing what a verdict-class mapping used to normalize. |
| `scheduler.py` | Three mentions — two are `active=false` guard comments (correct), one (line ~1310) claimed the table "keeps" being written — **corrected in this push**, see below. |
| `app_check_endpoints.py` | One historical-context line in the module docstring, already correctly past-tense (`"The app Check page READ /api/mobile/check, which SELECTed..."`). |

No live `SELECT` against `tc_screener_cache` exists anywhere outside the retired handler's own
dead code (the `CREATE TABLE IF NOT EXISTS` / `INSERT` inside `run_tc_screener_precompute`,
which is registry-gated `active=false` and never runs).

## Item 2 — `screen-cached` and `movers`: consumer check, boundary-aware, re-run fresh

| Route | Live callers found | Current state |
|---|---|---|
| `GET /api/trade-check/screen-cached` | **0** | Retired to `{cached:false, retired:true, ...}`, route wired. Correct per the card ("no consumer -> 410-shaped retirement"; this predates the literal-410 convention cc#1984 later established, but the effect is the same — a clear non-data answer, not a stale number). |
| `GET /api/trade-check/movers` | **1** — `scorr_check.html:2708` | **Migrated**, not retired, correctly — it has a live caller. Reads `tc_universe_ticks`, diffs the best-of-bucket `score100` between the two most recent trading days, maps `LONG`/`SHORT` to the table's `BUY`/`SELL` exactly as `app_check_endpoints.py` does for the same side convention. |
| `POST /api/admin/run-tc-screener` | **0** | Retired to a plain `{ok:false, retired:true, ...}` body, route wired. `run_tc_screener_precompute()` itself is untouched, per `do_not_touch` — `scheduler.py` still wires it to the `bg_tc_screener_precompute` row, which is `active=false` in `scheduler_master`. |

## Item 4 — the stale-comment sweep

**One live hit, corrected in this push.** `scheduler.py`'s `_bg_tc_screener_v2` docstring said,
present tense, *"The 16:00 old-path job **keeps running** and **keeps writing** the archive
table."* Checked against `scheduler_master` today: `bg_tc_screener_precompute` (the 16:00 job it
describes) is `active=false` — deactivated by cc#1862 (PR #161, sha `1b21ef24`) — and
`bg_tc_screener_v2` itself (the job this very docstring belongs to) is **also** `active=false`.
The comparison the comment describes happened, served its purpose (catching the exact
ENGINE_LIVENESS_RULE failure the docstring documents), and both sides of it are now off.

The historical narrative — why the job was built, what it caught, the ENGINE_LIVENESS_RULE
lesson — is **kept**, not deleted: deleting it would lose the lesson the same way the false
"keeps running" line would have kept telling it. Only the present-tense claim is corrected to
past tense, with today's live `scheduler_master` state stated plainly.

No other instance of the "`bg_tc_lite` writes `tc_screener_cache` every session" claim (the one
`mobile_endpoints.py` carried twice, both already fixed under cc#1984) was found anywhere else.

## Verify (per the card)

- Committed diff: this push, one file (`scheduler.py`), comment-only.
- `grep tc_screener_cache` on main after this push: no live `SELECT`, confirmed above.
- `movers` parity: `scorr_check.html` reads the same `score100` / `verdict10` fields
  `app_check_endpoints.py` reads from the same table, same side mapping — same shape already
  verified for `v12_endpoints.py` under cc#1982 (RELIANCE 88.5 / TCS 75.3 / NATIONALUM 53.5 /
  PNB 61.0 parity, Fable-confirmed on main).

`ast.parse` clean. Nothing under `worker/**`. This card's window has no recompute constraint
(no live-engine write), so it lands any hour outside 00:00–06:00 IST — pushed 08:45 IST.
