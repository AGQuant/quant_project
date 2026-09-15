# cc#2096 — Yahoo live quote endpoint fix

**Found during cc#2094's own first-run self-test, not a separately-filed bug report.**

## The problem, with real evidence

`yahoo_live_quote.fetch_live_quotes()` (cc#1417) called `query1.finance.yahoo.com/v7/finance/quote`
with only a `User-Agent` header. That module's own docstring already flagged this exact call as
**"NOT LIVE-VERIFIED FROM THIS SESSION... confirm the first real batch call once deployed"** — this
sandbox cannot reach Yahoo either, so cc#1417 shipped on faith.

cc#2094 (`equity_cmp_poll.py`) reused this function per its own spec and got the first real
production evidence: a self-test against the real 52-symbol equity universe returned
`quoted=0` — **zero of 52 real symbols resolved, no exception raised.** A silent empty result
(not a network error) is the well-documented failure signature of `v7/finance/quote`: that
endpoint has required a crumb+cookie handshake industry-wide since roughly 2022 for
unauthenticated callers.

## Confirming the direction, before touching anything

Checked the codebase's other two Yahoo integrations for comparison — `yahoo_daily_update.py` and
`yahoo_symbol_resolver.py`. **Neither ever uses `v7/finance/quote`.** Both use
`v8/finance/chart/{ticker}`, confirmed reachable in production (that endpoint is what
`yahoo_daily_update.py`'s nightly EOD backfill runs against every day).

## The fix

`fetch_live_quotes()` now calls `v8/finance/chart/{ticker}` — one request per symbol (that
endpoint isn't batchable, unlike `v7/quote`'s comma-separated batch) — fetched **concurrently**,
reusing `yahoo_daily_update.py`'s own already-proven-in-production settings verbatim rather than
guessing new ones: `SEMAPHORE_DEFAULT=3` concurrent requests, `SLEEP_DEFAULT=0.4`s pacing.

Price/OHLC come from the chart response's `meta` object (`regularMarketPrice`, `previousClose`,
`regularMarketDayHigh/Low` — long-stable, public Yahoo fields) with a fallback to the latest daily
bar's close/open/high/low when `meta` is thin — same principle as before: a symbol that can't be
resolved is **absent from the result**, never fabricated, never zero-filled.

**Return shape is unchanged** (`{symbol: {price, prev_close, open, high, low, chg_pct, asof,
source}}`) — both existing call sites needed zero changes:
- `mobile_home2.py`'s index tile (`fetch_live_quotes(["NIFTY50","BANKNIFTY"])`) — reads
  `price/open/high/low/prev_close`.
- `mobile_home2.py`'s ADR/breadth fallback (`fetch_live_quotes(_universe)`, ~208-212 symbols) —
  reads `price` only.
- `equity_cmp_poll.py` (cc#2094) — reads `price`.

## Trade-off, stated plainly

`v8/chart` isn't batchable, so the ~208-212-symbol ADR/breadth call now takes roughly 1-2 minutes
per outage-triggered request instead of returning instantly with nothing. That's a real latency
cost — but it only fires during an actual Fyers outage (rare), and it now actually works, versus
the prior behavior of returning nothing, silently, always. Flagged as an open tuning question if
it proves too slow in practice (smaller universe, higher concurrency) — not resolved here since
there's no real-outage evidence yet to tune against.

## Verification

This sandbox cannot reach Yahoo (confirmed blocked, same as before). What was verified: imported
the real, committed `yahoo_live_quote.py` directly and exercised its own parsing/concurrency logic
against realistic mocked `v8/chart` responses (httpx stubbed at the transport layer, matching
Yahoo's real, long-documented response shape):

| Test | Result |
|---|---|
| Empty symbol list → `{}`, zero HTTP calls | PASS |
| RELIANCE resolved from `meta` directly (price/prev_close/high/low exact, chg_pct computed correctly) | PASS |
| NIFTY50 → `^NSEI` index ticker mapping resolved via `meta` | PASS |
| ADANIENT with **thin meta** (no `regularMarketPrice`) — correctly fell back to the latest non-null bar close, skipping a `None` mid-series | PASS |
| Empty `chart.result` and an unmatched ticker — both correctly **absent**, never fabricated | PASS |
| Concurrency (semaphore + `asyncio.gather`) resolved all symbols without deadlock or silent drops | PASS |

This proves the module's own logic is correct. It does **not** prove Yahoo's real endpoint is
reachable from Railway's production egress — that needs a deployed self-test, armed immediately
after this lands (re-arming cc#2094's existing `equity_cmp_poll_run` gated trigger), with the real
`quoted` count checked on the next heartbeat tick.

## What did NOT change

`fyers_eq_outage()`'s detection logic (still reuses `feed_guardian`, untouched), the `SOURCE_TAG`
convention, the module's scope (index + ADR fallback only, still not futures/options CMP, still
not V8's own signal inputs).
