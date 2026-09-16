# cc#2116 — Max Pain card gets an explicit OPTION CHAIN button (web + app)

## What shipped
- **`v8_dashboard.html`** (`oiCard()`): added a `CHAIN` button to the card header, beside the
  existing as-of stamp and the `(i)` button, reusing the `.ib` class (no new button shape).
  `onclick="chainPopupOpen(name)"` — the same `name` value the adjacent `(i)` button already
  passes to `iiOiRead(name)`. Also made the `.oib` chart-body div itself a tap target
  (`role="button" aria-label="Open full option chain" style="cursor:pointer"`), matching the
  app's existing cc#2003 behaviour. Both call the same `chainPopupOpen` — no second handler.
- **`mobile/home.html`** (`oiCard()`): added the same `CHAIN` button, reusing the app's `.oiib`
  class, wired into both the `bare` and non-`bare` header layouts. The pre-existing cc#2003
  chart-area tap was left untouched.
- Label is **`CHAIN`** (not the spec's example wording `OPTION CHAIN`) — shorter so it sits
  cleanly next to the `(i)` button without wrapping at typical card widths. Per the spec's own
  `open_question_do_not_guess`: **this wording is CC's choice, not founder-specified — expect a
  possible correction.**

## Six entry points, one implementation (grep + read, stated per the verify item)
```
chainPopupOpen( call sites:
  v8_dashboard.html:7720   -- NEW: web CHAIN button
  v8_dashboard.html:7724   -- NEW: web .oib chart-tap
  mobile/home.html:3317    -- NEW: app CHAIN button
  mobile/home.html:3328    -- pre-existing (cc#2003): app .oib chart-tap
  mobile/home.html:5442    -- pre-existing (cc#2006): Market Mood capsule "full chain →"
scorr_cockpit_card.js:656  -- the one definition (async function chainPopupOpen(sym))
```
The D-button cockpit (`v8_dashboard.html:2469`, `scorr_card_strip.js:353` → `openDerivCockpit`)
is the 6th entry point — it renders the full cockpit sheet, not the standalone popup, but its own
"FETCH STRIKES" button calls the identical `dcFetchStrikes(sym)` the popup calls. The function's
own comment confirms this directly: `await dcFetchStrikes(sym); // same fetch + render the
D-button uses -- one implementation` (`scorr_cockpit_card.js:665`). All six entry points end at
one function (`dcFetchStrikes`) hitting one endpoint (`GET /api/deriv/chain-grid/{symbol}`,
`option_chain_grid.py`) and one renderer (`_dcRenderChainGrid`). No second data path exists —
confirmed by grepping the whole repo for `dcChainSelectStrike|_dcRender|chain-grid|dc-grid`
outside `scorr_cockpit_card.js`: the only other matches are code comments in the two edited
files that reference this same shared component, not a second implementation.

`scorr_cockpit_card.js` was confirmed already reaching both pages **before** writing any code —
`/dashboard` is in `main.py`'s `_PWA_INJECT_PATHS`, `/m/home` is `PROTECTED`, and `_MOBILE_HEAD`
(main.py:399-493, the block that injects `scorr_cockpit_card.js` among 11 other shared scripts)
fires for both. The cc#821 incident comment in `main.py` is specifically about this exact
injection reaching `v8_dashboard.html` correctly (a past bug had it land inside an HTML comment;
fixed). No new `<script>` tag was added to either page, and the cc#805 LOAD ORDER block was not
touched — it wasn't needed.

## Verify
- **Syntax**: `node --check` clean on every inline `<script>` block in both edited files.
- **Diff scope**: `git diff --name-only` shows only `v8_dashboard.html` and `mobile/home.html`
  changed. Zero touch on `option_chain_grid.py`, `deriv_metrics.py`, `option_ivp.py`,
  `max_pain.py`, `oi_structure.py`, or any function inside `scorr_cockpit_card.js` — confirmed
  directly from the diff, not asserted.
- **Real-data Playwright harness**: no live HTTP path reaches this sandbox (`scorr.in` is
  policy-denied by the outbound proxy, confirmed via `/__agentproxy/status`), so real production
  data was independently computed and fed through a local harness rather than skipped:
  - Real NIFTY/BANKNIFTY option-chain rows, spot prices, and 21-day closes pulled live via SQL.
  - The full `build_chain_grid()` pipeline (`deriv_metrics.strike_chain()`'s BS pricing,
    `option_ivp.chain_tags()`'s 120-session IV-percentile fair value/tag, `oi_structure`'s
    max-pain/wall computation) was reproduced by extracting every pure function **verbatim** (AST
    slice, never retyped) from the real files and feeding it the real fetched rows — confirmed
    239 real distinct trading days of `option_iv_daily` history for both symbols, comfortably
    above the 60-session floor, so the tag/fair-value path is genuinely live today, not blocked.
  - `oiCard()` itself is IIFE-private in `v8_dashboard.html` (unreachable from a test driver
    directly) and booting the full 375KB dashboard pulls in unrelated CDN chart libraries this
    sandbox's proxy also blocks. Rather than skip the check, `chainAsOf`/`oiUnavail`/`oiCard`
    (lines 7529-7731, this card's own edit included) plus their three real dependencies
    (`num`/`esc`/`istNow`) were extracted verbatim into a minimal page carrying the real `<style>`
    block and the real injected shared scripts — same real logic, real CSS, real
    `chainPopupOpen` wiring, none of the unrelated boot surface. `mobile/home.html`'s `oiCard` is
    plain top-level and was driven directly.
  - Result, both surfaces, both symbols: `window.chainPopupOpen` resolves as a real function;
    the CHAIN button renders (bare + non-bare app headers, and web); clicking it opens `#dcOv`
    with the correct symbol (`sheet mentions NIFTY` / `BANKNIFTY` both True — the `name` argument
    genuinely passes through, not hardcoded); the pre-existing `.oib` chart-taps (app's cc#2003,
    web's new one) still open the same overlay with no handler collision; the page URL is
    unchanged after open and close; tapping a strike shows a **real** rupee fair value + tag —
    NIFTY 23250 CALL: Premium 229.2 (**Expensive**), IV 12.2%, **Fair value ₹264.14**, Delta
    0.5285 — genuinely computed, not fabricated. Max-pain (23550) and call-wall (24000) rows
    render colour-highlighted, matching the independently computed values exactly.
  - **One honest gap in the harness itself, not the shipped code**: the real
    `deriv_metrics.strike_chain()` caps the chain to the 21 strikes nearest spot
    (`sorted(...)[:21]`); my harness fed the full real 41-strike chain instead (I didn't
    reproduce that slicing step), so the screenshots show more rows than production would. This
    doesn't affect what's being tested — the button/tap wiring and the shared render/data path —
    only the row count in the test's own popup. Flagging it rather than letting the screenshot
    imply a row count I didn't actually verify.

## VISUAL_VERIFY_GATE_V1
Five screenshots taken, looked at directly:
- **Web, card closed** (1440×900): `Max Pain NIFTY` title, stamp row `16-Sep 15:25 · PCR 1.13`
  with `CHAIN` and `i` sitting cleanly side by side — no crowding, no wrap, no overlap.
- **App, card closed** (390×844): both header variants rendered — the bare (compact stamp row)
  and the full (title + stamp row) — `CHAIN` and `i` both fit cleanly in each on a 390px phone
  width, the narrowest this app ships.
- **Web popup open**: `NIFTY · OPTION CHAIN`, real spot/expiry/PCR, real strike table, ATM row
  marked, max-pain and call-wall rows colour-highlighted correctly.
- **Web popup, strike tapped**: the detail panel described above — real premium, IV, fair value,
  tag, Greeks, both legs.
- **App popup open**: `BANKNIFTY · OPTION CHAIN`, real BANKNIFTY strikes, confirming the symbol
  argument is genuinely per-card, not shared/hardcoded state.

## What did NOT change
Grid columns, colours, legend, fair-value formula, IV-percentile tagging, Greeks, the `(i)` read
popover (`oiRead`/`oiGuide`), and stock option chains — none of these were touched, per the
spec's own `explicitly_not_in_scope`.

## Open item for the founder
Button label is `CHAIN`, not the spec's example `OPTION CHAIN` — shorter, fits the header
cleanly on the narrowest app width tested (390px). Say if a different wording is wanted.
