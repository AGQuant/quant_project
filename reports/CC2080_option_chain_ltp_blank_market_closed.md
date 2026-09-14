# cc#2080 (normal) — D-button Option Chain: stock CE/PE LTP a bare dash on every row

Founder screenshot (14-Sep-2026, a market holiday): the D-button stock Option Chain (06 OPTIONS —
STRIKE CHAIN, spot 1831.1, exp 2026-09-29). STRIKE populated real numbers (1640 through 1800+) but
CE LTP and PE LTP showed a dash on every single row, with no explanation.

The card arrived with the root cause already precisely diagnosed and offered two fix options to
choose between. My Step 1 gate confirmed that diagnosis by reading the actual data-access code,
which also settled the choice and surfaced one extra, closely-related gap in the option offered.

## Root cause — confirmed by reading the code, not re-derived

`deriv_metrics.strike_chain()` has two branches. The **INDEX** branch (NIFTY/BANKNIFTY) reads
`ltp` straight from the stored `option_chain` DB table — a value that survives after the feed
stops, so it is structurally immune to this bug. The **STOCK** branch (this screenshot — the file's
own OI/walls note already says "index only", confirming it's a stock chain) calls
`_batch_quotes()`, a pure live Fyers REST quote fetch with **zero DB fallback anywhere** — a ticker
simply never enters the result dict when there's nothing live to return. **Confirmed via
`option_chain_grid.py`'s own docstring: no stored stock-option-LTP table exists in this codebase
at all.**

That rules out the card's **Option A** (a stored last-known-LTP fallback) — there is nothing to
fall back to. **Option B** (an explicit message when the market is closed, instead of a bare dash
grid) is the only honest choice, and is what shipped.

**One widening from the card's own Option B wording, stated plainly:** the card frames Option B
around "the market is closed" (its own example: a holiday). But `_batch_quotes()` is blank for the
exact same structural reason — no live market — on an ordinary trading day too, before 09:15 or
after 15:30 IST, not only on a notified holiday. Gating the message on a day-only check
(`is_trading_day` alone, as the card's own suggested check reads literally) would still leave a
silent, unexplained dash grid for hours every trading day, the identical anti-pattern the card
exists to fix. The shipped check covers day **and** hours together — one condition, the one
`_batch_quotes()` actually depends on — not a second, different option.

## Fix

**Backend — `deriv_metrics.py`:**
- New `_market_open_now(now_ist=None)`: `nse_holidays.is_trading_day()` (the same canonical gate
  `main.py`/`scheduler.py`/`guards.py` already use — imported, not re-implemented) AND
  09:15–15:30 IST, both bounds inclusive. **Pure — no I/O — so a test can pass any clock**,
  mirroring `guards.py`'s own `approval_window()` convention verbatim (its docstring literally
  says this).
  `MARKET_OPEN`/`MARKET_CLOSE` as local module constants matches this codebase's own established
  idiom — `price_resolver.py`, `fyers_backfill.py`, `mobile_endpoints.py`, `worker/fyers_feed.py`
  each keep their own local copy rather than importing a shared one; this follows the same pattern
  rather than inventing a new one.
- The STOCK-path return payload only gains one key: `"market_open": _market_open_now()`. The
  INDEX-path return is untouched — it doesn't need the field and adding it there would wrongly
  imply this bug touches that path too.
- Field name `market_open` is not invented — it's `mobile_endpoints.py`'s own existing field name
  for the identical semantics (`is_td and MARKET_OPEN <= t <= SESSION_END`), reused for
  consistency across the codebase's payloads.

**Frontend — `scorr_cockpit_card.js`, `_dcRenderChainGrid()` (the one D-cockpit chain-grid
renderer):**
- `d.market_open === false` (strict, never falsy — an index payload simply never carries the key,
  so this must not misfire on `undefined`) renders one notice, in the same box style
  `_dcChainInfoHtml()` already uses (confirmed-bridged tokens only — `var(--c-panel)`,
  `var(--c-bd)`, `var(--c-mut)`, the same three this file's own comments already flag as the only
  ones proven to exist on this theme bridge): *"Market is closed right now — CE/PE LTP is a live
  quote only, so there is nothing to show until trading resumes."* Placed above the table, existing
  header/hint line and table structure otherwise untouched.
- The table itself still renders every strike row — the dash cells stay exactly as they already
  were (`_dcLtpTxt`'s existing `&mdash;` for a null LTP). This explains the dash, it does not hide
  or replace it.
- **One file, both surfaces**: `_dcRenderChainGrid` is the one shared renderer for the D-cockpit
  chain, the standalone Home popup (cc#2003/cc#2006), and `mobile/home.html`'s own chain-grid tap
  target — confirmed directly (not just by the file's own comment) that `mobile/home.html` never
  hand-loads a second copy: `/m/home` is in `PROTECTED`, and `auth_gate`'s `_MOBILE_HEAD` injects
  `scorr_cockpit_card.js` into every protected page's head, so this fix reaches both places by
  construction, zero risk of the two drifting apart.

## Scope_note — confirmed, not assumed

The card asked me to confirm the index path is unaffected before closing, since that wasn't
directly checked in its own diagnosis. Confirmed two ways: (1) reading `strike_chain()`'s INDEX
branch — `ltp` comes from a stored `option_chain` row, not `_batch_quotes()`; (2) a source-level
test (`inspect.getsource`, see Verify) that mechanically checks the INDEX branch's own return
statement never carries `market_open` and the STOCK branch's does — not a read of the code once,
but a repeatable check against the actual shipped file.

## Verify

`py_compile` clean on `deriv_metrics.py`. `node --check` clean on `scorr_cockpit_card.js`.

**Backend — 16/16 checks**, `_market_open_now()` is pure, so every case is tested directly, no
live DB or network needed:
- **Real data anchor**: `nse_holidays.is_trading_day(2026-09-14)` is `False` — the card's own
  reported day — and `_market_open_now()` with the live clock agrees.
- Every boundary on a real trading day (2026-09-11): mid-session `True`; the open/close edges
  09:15:00 and 15:30:00 both inclusive (`True`); one second either side of each edge (`False`);
  well before/after hours (`False`).
- A real weekend day and the holiday itself at mid-session hours — both `False` (the day gate wins
  even inside the hour window, confirming day and hours are genuinely ANDed, not one overriding
  the other silently).
- `inspect.getsource(strike_chain)` split on the two branches: STOCK return carries
  `market_open`, INDEX return does not — the scope_note claim checked against the real file, not
  restated from memory.

**Frontend — 10/10 checks** on the new `closedNotice` line, extracted verbatim, real headless
Chromium: renders on `market_open===false`, states the real reason (a live quote, not a fabricated
number — checked against the VISIBLE TEXT with markup stripped, not the raw HTML which legitimately
contains CSS digits), stays silent on `true`, on `undefined` (the index shape), and on `null`
(strict `===false` only, not merely falsy).

**Frontend — 13/13 end-to-end regression**, the *real* `_dcRenderChainGrid()` plus its real
dependencies (not a stand-in), three realistic payloads in real headless Chromium:
1. Stock, market shut, three strikes, no live quotes — notice renders, all three strike rows still
   render, dash cells intact, no OI columns (`oi_available=false`, cc#2023, untouched), the
   existing "index only" OI legend note still shows (cc#2034, untouched).
2. Stock, market live, real quotes — **no notice, no regression**: LTP values render correctly
   through the existing `.toFixed(1)` formatting (cc#2019).
3. Index payload (`market_open` key never sent) — no notice, OI columns present, real max-pain/
   wall legend shown (cc#2019/cc#2023/cc#2034 all untouched) — the scope_note's claim re-confirmed
   end-to-end, not just at the source level.

Two over-strict assertions of my own were caught and fixed along the way, both test-script
artifacts, not product bugs: a DOM-round-trip check for the raw `&mdash;` entity text where the
browser had already decoded it to the literal em-dash character on `.innerHTML` read-back; and a
fixture LTP (38.15) asserted verbatim where the code's own, correct, pre-existing `.toFixed(1)`
rounds it to 38.1 (confirmed directly: `node -e "(38.15).toFixed(1)"` prints `38.1`, not the
fixture's raw input).

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that a stock D-button chain today shows the new message, and that the same chain on the
next live trading day shows real LTP numbers exactly as before.
