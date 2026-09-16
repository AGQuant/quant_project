# cc#2120 Part A — last-traded FUTURES/spot reference price in the Approve popup (P1)

## The gap, confirmed by reading the code before building anything
`/api/tradewall/prefill-levels` (trade_wall_endpoints.py) returned only
`{engine, symbol, target_price, stop_loss}` — no current price of any kind. The founder's own
screenshot (CAMS SELL, V8, qty 825) showed the Approve popup asking for a target and a stop with
zero reference price on screen. Exactly the gap the card names first.

## What was built
**cmp_resolver.py** — new `resolve_fut_cmp(cur, symbol)`, appended after `resolve_cmp_many`.
Reads ONLY `intraday_prices` `source='fyers_fut'` `timeframe='5m'`, same return shape as
`resolve_cmp` (`{cmp, prev_close, day_pct, source, ts, live}`). No fallback tier inside it — a
symbol with no futures bar returns `cmp=None, source='NO_FUT_BAR'`, and the CALLER decides the
fallback. This is deliberate: resolve_cmp's cache/Yahoo/STALE tiers all bottom out on a spot
number, and letting this function fall back internally would silently reintroduce the exact
mixed-leg bug this whole card exists to fix.

**trade_wall_endpoints.py** — `/api/tradewall/prefill-levels` gains an `instrument` query param
(FUTURES/EQUITY, the SAME field every wall row already carries client-side — not re-derived from
futures_universe membership, because a symbol's own F&O eligibility is not the same question as
which instrument THIS signal is on). Two new helpers:
- `_reference_price(cur, sym, inst)`: FUTURES → `resolve_fut_cmp`; no bar → fall back to
  `resolve_cmp` (spot) but label it `"spot (no futures bar)"` with an explicit `note`, never
  silently shown as futures. EQUITY → `resolve_cmp` directly, labelled `"spot"`.
- `_stale_futures_bar(ts)`: True only when a futures bar is >15 min old AND the market is
  trading right now (`nse_holidays.is_trading_day` + `nse_session.MARKET_OPEN/MARKET_CLOSE`,
  both existing canonical modules, not reinvented). Uses `_ist_now()` (already imported in this
  file) on both sides of the subtraction — naive IST vs naive IST, the same convention
  `intraday_prices.ts` uses, deliberately avoiding the naive-vs-timestamptz mismatch this exact
  card's own ground-truthing against `trade_alerts.approved_at` hit earlier this session.
- `_ref_block(...)`: the DAY-PRECISION HONESTY RULE (see below) — adds `ts_precision`.

**trade_wall_web.html / mobile/trade_wall.html** — both Approve popups get a new
`<div id="apLvlRef">` line directly under the existing status message, above the Target/Stop
inputs. `apLvlOpen()` now passes `&instrument=` (from the row's own `e.instrument`) and populates
the line via a new `apLvlRefTxt()` on each surface, matching each file's own idiom (web: `px2`/
`twStampTime`; app: `f2`, inline slice — copied, not shared, since the two files have never
shared JS and this card doesn't change that).

## A day-precision bug caught by the verification harness, not shipped
Building the real Playwright harness (see below) surfaced a bug before it reached main: the
STALE fallback tier's `ts` is `raw_prices.price_date` — a DATE, no time component. Handing a bare
`"2026-09-15"` to the web surface's existing `twStampTime()` (`new Date(...)`) parses it as UTC
midnight, which then prints as **"05:30 IST"** — a clock time that never happened. The mobile
surface's char-slice approach produced a different but equally wrong artifact (a dangling
`"at  IST"` with nothing before it, since slicing characters 11:16 off a 10-character string
yields `""`). Both are exactly the failure mode this file's own documented DAY-PRECISION HONESTY
RULE exists to prevent (stated further up in this same file for the wall's event union: "a
day-precision event... prints its date only, never a fabricated clock time").

Fixed at the source: `_ref_block` now stamps `ts_precision` (`"minute"` when the underlying value
has a time component, `"day"` when it's a bare date). Both frontends check it and print
`"on YYYY-MM-DD"` instead of a fabricated `"at HH:MM"` for a day-precision value. Re-verified
after the fix — see below.

## What did NOT change (do_not_touch, checked by diff not by memory)
`git diff cmp_resolver.py` is a **pure append** — `SPOT_SOURCES`, `resolve_cmp`,
`resolve_cmp_many` show zero removed/changed lines above the new function. The card's own test —
"if the diff touches SPOT_SOURCES, it is wrong" — is satisfied by inspection, not assertion.
`approve_signal`, `approve_alert`, the cc#2027 two-call approve sequence, the approval window
gate, and `trade_alerts` schema are all untouched — Part A is a read-only display addition, per
the card's own scope.

## Verification

**Syntax**: `ast.parse` + `py_compile` clean on both edited `.py` files. `node --check` clean on
every changed inline `<script>` block in both edited `.html` files (extracted verbatim).

**Real SQL cross-check, to the paisa, three symbols, mid-session** (server clock confirmed live
via `server_now`: 2026-09-16 12:58 IST, market open). `resolve_fut_cmp`'s query is
`SELECT close, ts FROM intraday_prices WHERE symbol=%s AND source='fyers_fut' AND timeframe='5m'
... ORDER BY ts DESC LIMIT 1` — run directly against production for CAMS/DLF/LTM:

| Symbol | fyers_fut close | ts (IST) | age at check time |
|---|---|---|---|
| CAMS | 708.00 | 12:55:00 | ~3 min — mid-session |
| DLF | 625.00 | 12:55:00 | ~3 min — mid-session |
| LTM | 4330.80 | 12:55:00 | ~3 min — mid-session |

All three fresh, live, `stale=False` by construction (age well under 15 min). GRASIM (a real open
QB Basket equity position) cross-checked against `resolve_cmp`'s own tier-1 query
(`source IN ('fyers_eq','fyers_ext','fyers_hist')`): close 3192.6 @ 12:55:00, source tag `fyers`
— confirms the EQUITY path is labelled `spot`, never `futures`. 20MICRONS (confirmed via
`information_schema`-style query to have **zero** fyers_fut AND zero fyers_eq/ext/hist 5m rows)
used to exercise the no-futures-bar fallback: its only real price anywhere is the raw_prices EOD
close, **201.29 @ 2026-09-15** — the genuine STALE-tier value, not fabricated.

**Real-code Playwright harness, both surfaces**, extracting the actual edited functions
(`apLvlOpen`, `apLvlRefTxt`, `apLvlClose`, plus each surface's own real formatters) verbatim from
the edited files, real popup markup verbatim (including the new `apLvlRef` line), real design
tokens (`scorr_themes.css` `:root` + `body[data-theme="dark"]`, read directly rather than
approximated), mocked network layer returning the exact real values above. Browser context pinned
to `timezone_id="Asia/Kolkata"` — a headless sandbox otherwise defaults to UTC local time, which
would shift every displayed clock time by 5h30m purely as a test-environment artifact (caught,
diagnosed against the pre-existing unmodified `twStampTime`, and fixed in the test, not the
shipped code). Checked at 360/390px, plus one representative screenshot at 375px per state:

- **Web, CAMS (FUTURES)**: `"CAMS futures, last traded 708.00 at 12:55 IST"` — exact match.
- **Web, GRASIM (EQUITY)**: `"GRASIM spot, last traded 3,192.60 at 12:55 IST"` — labelled spot.
- **Web, 20MICRONS (FUTURES, no bar)**: `"20MICRONS spot (no futures bar), last traded 201.29 on
  2026-09-15"` — day-precision, no fabricated time, never claims to be futures.
- **App, CAMS**: `"CAMS futures, last traded 708.00 at 12:55 IST"` on the bottom-sheet shell.
- **App, GRASIM**: `"GRASIM spot, last traded 3192.60 at 12:55 IST"` (app's `f2()` has no
  thousands-grouping, unlike web's `px2()` — matched exactly, not assumed identical).
- Fold check at all three widths, both surfaces: Confirm/Cancel bounding boxes fully inside a
  700px (web) / 667px iPhone-SE-height (app) viewport in every case — the new line adds one row
  of text, nowhere near pushing the buttons off-screen.
- Zero uncaught page errors across every open, every viewport, both surfaces.

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1**:
- `cc2120_web_futures_375.png` / `cc2120_web_equity_375.png`: centred dialog, reference line
  directly under the status message, correctly labelled, Confirm/Cancel fully visible with room
  to spare below.
- `cc2120_mobile_futures_375.png` / `cc2120_mobile_equity_375.png`: bottom sheet (the app's own
  idiom, distinct from the web's centred dialog, exactly as the existing code comment says it
  should be), same reference line, same labelling, buttons fully visible above the fold.

## Sequencing
Part A lands as its own push now, per the card's explicit instruction. Part B (the
`resolve_fut_cmp` wiring into `approve_signal`/`approve_alert` and the wall's P&L display, plus
the 3-row `approved_price` re-stamp) follows as a separate push.
