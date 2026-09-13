# cc#2043 — Universal rule: any chart/C button's one-day view fetches on demand from Yahoo

Founder directive, 13-Sep-2026, widened from a GVM-only ask to an app-wide standing rule: wherever
a chart/C button's "1 day" view exists, it fetches intraday bars on demand from Yahoo Finance —
never Fyers, never a subscription, never a database write, request-scoped to the tap alone.

## The one canonical function — reusing this project's own existing Yahoo client, not reinventing it

`yahoo_ondemand.py` (`fetch_intraday(symbol, days, interval)`) already does exactly what the rule
needs — a direct, no-DB-write, no-fabrication Yahoo chart-API client, already depended on by
`cmp_resolver.py`, `gvm_market_endpoints.py`, `main.py` and `yahoo_index_backfill.py`. **Two
already-existing endpoints were deliberately NOT reused**, checked by reading each one's own
docstring before choosing: `/api/intraday_ondemand/{symbol}` supports `source='auto'/'fyers'` (the
opposite of "never Fyers"), and `ondemand_bars.py`'s `/api/bars/{symbol}` states outright *"FYERS
FIRST, YAHOO SECOND"* — reusing either would silently violate the rule's own explicit constraint
the first time a caller forgot to pass the right parameter. **`chart_oneday_endpoints.py`** wraps
`yahoo_ondemand.fetch_intraday` directly instead: `GET /api/chart/oneday/{symbol}` →
`{symbol, source:'yahoo', session_date, bars, note}`, Yahoo-only by construction, no Fyers call
possible in its own code path. Pulls a few days of buffer (a bare 1-calendar-day window can be
genuinely empty over a weekend/holiday) and returns only the **latest actual trading session's**
bars — the same session-anchor discipline this codebase already uses elsewhere (e.g.
`mobile_home2.py`'s own `MAX(ts)::date` anchor) — never fabricating a bar Yahoo did not return; a
thin or empty session is stated in `note`, not padded.

## Audited and wired: the shared chart card, reaching four surfaces at once

`scorr_chart_card.js` (its own comment: *"cc#752... every surface — V8, SmartGain, TC cards, GVM
'C' — shows the SAME timeframes"*) is the ONE existing chart component behind the "1D" pill,
loaded by `scorr_cio_dashboard.html` (**GVM**, this card's own named target), `scorr_digest_v3.html`,
`v10_dashboard.html` and `v8_dashboard.html` — fixing this one file reaches all four surfaces at
once, rather than four separate per-page changes.

**Before**: the 1D pill was hard-disabled (`opacity:.4, cursor:not-allowed`) for any symbol not on
the live futures feed, with the tooltip *"1D (5-min intraday) is available for F&O (futures) stocks
"* — a non-futures stock's 1D view was simply unreachable.

**After**: the pill is never disabled for this reason. `_load()` branches on the SAME `_futCache`
probe that already existed (`_probeFutures`, pre-cc#2043) — a futures symbol still calls the
original `/api/intraday/{sym}?sessions=5` path unchanged (byte-identical to before this card); a
non-futures symbol now calls `/api/chart/oneday/{sym}` instead, and its `{bars: [...]}` response is
unwrapped into the exact same row shape the existing candle-mapping code already expects, so
rendering, VWAP/VPOC, pivots and every other 5m-timeframe feature keep working unmodified — this
card only changed WHICH endpoint supplies the rows for a non-futures symbol, not how they are used
once fetched. An empty/thin Yahoo response shows its own honest `note` in the chart's message area,
distinguishable from the generic "no data" line the Fyers-backed path already had.

## Verify

`ast.parse` clean on `chart_oneday_endpoints.py` and `main.py`; `node --check` clean on
`scorr_chart_card.js`. **Live network to Yahoo is unreachable from this sandbox** (confirmed via a
direct `curl` — `connect_rejected`, organization egress policy, the same class of restriction that
already blocked Google Fonts elsewhere this session) — so `yahoo_ondemand.fetch_intraday` itself
(pre-existing, already-shipped, already depended on by four other modules) was not re-verified
against live Yahoo; **the new wrapper logic in `chart_oneday_endpoints.py` was unit-tested with that
one function mocked at the network boundary** — 5 cases: a multi-day fetch correctly narrows to only
the latest session (24 of a 25-bar synthetic response, discarding the older day); a thin 2-bar
session states the honest count in `note`, never pads; an empty Yahoo response returns an honest
empty result; a hard Yahoo failure surfaces as a 502 with the real underlying reason, not silently
swallowed; an empty symbol is rejected with 400 — all 5 pass.

`unpkg.com` (the chart-library CDN) is also unreachable from this sandbox (same policy) — real
headless Chromium loaded the actual, unmodified `scorr_chart_card.js` + `scorr_card_common.js`,
with `LightweightCharts` replaced by a minimal stand-in that **records exactly what candle data it
was handed** (this tests the real file's own branching/data-mapping logic, not chart rendering,
which is what's under test here) and `/api/intraday/*` / `/api/chart/oneday/*` intercepted via
`page.route`. **13/13 checks pass**:

- A non-futures symbol (`TATASTEEL`): the 1D pill is not disabled, its tooltip explains the Yahoo
  path, clicking it calls `/api/chart/oneday/TATASTEEL` (never the old `sessions=5` path), and the
  real fetched bars (3 candles) reach the chart-rendering call correctly mapped, matching the exact
  values served (not fabricated).
- A futures symbol (`RELIANCE`): the 1D pill behaves exactly as before this card — still enabled,
  clicking it still calls the original `/api/intraday/RELIANCE?sessions=5` path, never the Yahoo
  endpoint — confirming zero regression on the path this card was not meant to touch.
- Zero console errors across the whole interaction, once two entirely unrelated pre-existing calls
  this shared file already made on every `open()` (`/api/v8/futures/list` — a different,
  already-documented "^" futures-tradability marker, cc#1500; `/api/trade-check/fibcheck` — cc#845's
  own Fib/verdict check) were stubbed in the test harness, having nothing to do with this card.

## Not done here (explicitly out of scope, per this card's own text)

Wiring cc#2041's open-position chart or cc#2042's alert-card "C" button to this function — neither
exists yet (both are separately-tracked, currently-held cards); this card's own scope is auditing
**existing** chart/C buttons only, and the shared function is built so whichever of those cards
lands next reuses it rather than growing a second implementation. Any live/current-price display or
WebSocket behavior on any surface — this rule is the one-day **chart** fetch only, and touches no
subscription or `cmp_prices` write anywhere in its own code path (confirmed by inspection:
`chart_oneday_endpoints.py` imports only `yahoo_ondemand` and `fastapi`, nothing from
`worker/fyers_feed.py` or any DB module).
