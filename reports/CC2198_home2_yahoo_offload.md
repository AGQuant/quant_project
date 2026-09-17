# cc#2198 — /api/mobile/home2 walled at 31.5 s from 15:20 IST: the Yahoo fallback ran its 28 s sweep inside every request

P0, founder 17-Sep-2026 15:24 IST ("Loading your dashboard…" on a blank Home). Claimed 15:28 IST; the landing time is in the task row.

## Evidence (before any fix)

| Fact | Source |
|---|---|
| home2 272–443 ms through 15:19:30 IST, then 31.3–32.0 s on every call from 15:20:56 IST, all HTTP 200 | `perf_request_log`, path `/api/mobile/home2` |
| fyers_eq cash leg's last bar 15:10:00 IST; fyers_fut kept writing (15:25:00) | `intraday_prices` (naive IST timestamps), `MAX(ts)` per source |
| feed_guardian `STALE_MIN = 10` | `feed_guardian.py:44` |
| 208 active symbols | `futures_universe WHERE is_active` |
| Page gives up at 15 s | `scorr_card_common.js` `fetchWithTimeout` default 15000 ms |

So at 15:20 the cash leg was 10 minutes stale in market hours, `yahoo_live_quote.fyers_eq_outage()` turned True, and the cc#1417 Yahoo fallback in `mobile_home2.py` ran on EVERY home2 call: one Yahoo chart request per active symbol, `Semaphore(3)` and a 0.4 s sleep held inside each semaphore slot (`_fetch_one … finally: await asyncio.sleep(SLEEP_S)`). That is 208 ÷ 3 × 0.4 s ≈ 27.7 s for the breadth sweep, plus two index calls, plus the normal ~0.3 s: 28.5–32 s by construction, whether Yahoo answers or not. It also held a transaction open on `futures_universe` for the whole sweep (the idle-in-transaction session in the card's evidence). The page's 15 s fetch budget aborted every call, so Home could never render while the gate was on. The gate is market-hours only, so the symptom clears itself at 15:30; the design stayed broken for the next cash-leg stall.

One more thing found in the same block: it wrote the Yahoo index quotes to `idx[sym]`, one level above `idx["indices"]` where the response and the tape read them. The fallback index prices never reached the page. Fixed inside the rewrite.

## What shipped

| File | Change |
|---|---|
| `yahoo_live_quote.py` | `fetch_live_quotes(symbols, budget_sec=None)`: with a budget, `asyncio.wait(..., timeout)` returns whatever answered inside it and cancels the rest (absent, never zero-filled); without a budget it behaves exactly as before (the scheduler caller `equity_cmp_poll.py` is unchanged). |
| `mobile_home2.py` | The fallback never runs inside a request. A module cache (`_YF`) is filled by one daemon thread (`_yahoo_refresh`: the two indices first with a 6 s budget, cached the moment they land; then the 208-symbol breadth sweep, unbudgeted, off the request), each DB read on its own short connection, nothing open while Yahoo is called. The request only reads a snapshot: indices served if ≤ 5 min old (tagged with their own `asof`), breadth if ≤ 5 min old and ≥ 50 names resolved; a refresh is kicked when the index cache is older than 60 s and none is running; otherwise the last-known DB values already computed stand. New payload key `live_fallback` (`null` normally; `{engaged, fyers_eq_age_min, indices: yahoo_cache|last_known, adr: …, refreshing, idx_age_s, adr_age_s, last_error, last_run_s, note}` during an outage). Index quotes now land in `idx["indices"]`. |
| `mobile/home.html` | After 8 s on "Loading your dashboard…" the line becomes "Still loading… a live source is slow." with a Retry button (the request keeps running). At the 15 s abort the error reads "The dashboard took too long to answer (15 s). A live source may be slow -- try again." with Retry. When the payload carries `live_fallback.engaged`, one plain line sits on top of the deck: "Live cash feed paused N min ago · index and breadth figures are from the backup source / the last known values · refreshing". Rendered on the initial load and on every 5-min poll. |
| `tests/test_yahoo_live_budget.py` | 3 tests: a 0.5 s budget over nine 0.3 s calls returns exactly the three that finished and takes well under the list length; no budget returns all; empty list short-circuits. |
| `tests/test_home2_yahoo_cache.py` | 3 tests (no DB, no network): the refresher caches the indices with the hard budget and the breadth 40/15/5 from a 60-name sweep, a second kick while one runs is refused; a 30-name batch (< 50 floor) writes no breadth and an empty index answer caches nothing; a DB failure is recorded in `last_error`, never raised. |
| `reports/CC2198_home2_yahoo_offload.md` | This report. |

Spec item 3 asked for progressive section rendering as well; the deck renders from one payload today and that is unchanged. The 8 s notice, the honest 15 s message and the fallback line are the client part of this card.

## Harness (Playwright, real Chromium, `scratchpad/cc2198_test.py`) — ALL PASS at 375×812

`/api/mobile/home2` served by a local server with a controllable delay; everything else stubbed.

- Normal payload: the deck renders in 0.15 s (3 cards), no fallback line, no loading line.
- `live_fallback` last_known: the line "Live cash feed paused 10.5 min ago · index and breadth figures are the last known values · refreshing" is the first child of the deck; with `yahoo_cache`: "… from the backup source".
- 9.5 s endpoint: at 8.1 s the loading line reads "Still loading… a live source is slow." with a 44 px Retry button; at 10.0 s the deck renders and the line is gone.
- 25 s endpoint: at 15.5 s "Could not load — The dashboard took too long to answer (15 s). A live source may be slow -- try again." with Retry.
- No page errors in any case.

**Screenshots looked at** (`scratchpad/cc2198_*.png`): `375_dark_note` — the muted line under the header, above "The market today" card (dashes because the stub carries no figures), the deck and grid below. `375_light_slow` — "Still loading… a live source is slow." with the outlined Retry chip. `375_dark_dead` — the "Could not load" box with the 15 s sentence and Retry.

## Timing after the fix

Unit tests: 6 passed. Wall clock of the request path during an outage = the normal path + one `_leg_ages` query + a lock snapshot; the Yahoo work is on the thread. The production numbers (spec verify item 4, `perf_request_log` p95 over 10 calls) are read after the deploy and written in the task result; note the gate is off after 15:30 IST, so post-close calls exercise the normal path.

## Not touched

The fyers_eq stall itself (the cash leg stopped at 15:10 IST while the futures leg kept writing) is a feed-worker matter, `worker/**`, market hours — filed as its own card, not touched here. cc#2170 nav and cc#2185 tile work untouched.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `curl -w '%{time_total}' https://scorr.in/api/mobile/home2` × 5 → each under 1 s; `live_fallback` present in the JSON (`null` after 15:30).
2. Next cash-leg stall in market hours: home2 stays fast, the page shows the "Live cash feed paused" line, and the app log shows "home2: fyers_eq leg stale … Yahoo fallback from cache" rather than a 30 s wall.
