# cc#2004 — frontend: the NSE-style chain grid, wired into the D-button

Second push. First push (sha `2557e66`) landed the shared backend (`option_chain_grid.py`). This
lands the actual grid UI in `scorr_cockpit_card.js`'s section 06 (OPTIONS · STRIKE CHAIN), the
D-button's own primary target per the card's item 11 (explicitly not Index Intel).

## What this lands

- **Endpoint switch**: `dcFetchStrikes()` now reads `/api/deriv/chain-grid/{symbol}` instead of the
  old `/api/deriv/strike-chain/{symbol}` — same underlying ltp/iv/fair/tag rows (`strike_chain()`
  is that new endpoint's own first step, untouched), now also carrying per-strike OI and
  max-pain/call-wall/put-wall flags when the symbol is an index.
- **Full inside borders** (item 2): every cell gets its own `border:1px solid var(--c-grid)`,
  replacing the old row-only hairlines.
- **Tag dot** (item 4): a small coloured dot next to each leg's LTP, reading cc#1859's own
  `ivp.tag` (CHEAP/FAIR/EXPENSIVE, percentile-banded) specifically — **not** the older
  Black-Scholes `tag` field (EXPENSIVE/REASONABLE/CHEAP), which the detail panel still shows
  separately, clearly labelled "Fair (BS σ=RV20)" so nothing is silently dropped. No tag yet
  (outside cc#1859's 60-session floor or its sanity gate) draws an empty dot slot, never a
  fabricated colour.
- **Max pain / call wall / put wall** (items 5/6, revised 12-Sep to full-row colours): gold / coral
  / teal row backgrounds via `color-mix()`, reading the new `is_max_pain`/`is_call_wall`/
  `is_put_wall` flags the backend merges in. A row that is both max-pain and a wall (checked in the
  synthetic test) renders max-pain's colour — stated as the precedence rule in the code, since the
  card doesn't specify what happens when two coincide.
- **Tap a row → inline detail panel** (item 7): both legs where present, showing premium, IV, IVP
  (percentile + tag), **two** distinct fair values (Black-Scholes and cc#1859's own IVP fair value
  — shown separately rather than guessing which one the card meant by "fair value"), OI, and the
  as-of timestamp. OI change and bid/ask are shown as explicit **"not tracked" / "not captured"**
  lines rather than omitted silently or fabricated — grounded in the backend push's own findings
  (no OI-change baseline exists; `option_chain.bid`/`.ask` are never populated, checked live).
  Tapping the same row again closes the panel.
- **(i) button** (item 8): top-right of the chain's own mini-header, toggles a plain-language
  explainer (dot colour, max pain, call/put wall) — inline, not a separate screen.
- **Legend row** (item 9): colour-dot key always shown; the max-pain/call-wall/put-wall key only
  when `oi_available` — for a stock it instead states plainly "OI / wall / max pain: index only —
  not available for a stock chain," never blank, never a fabricated mark.
- New inline-handler functions (`dcChainSelectStrike`, `dcChainToggleInfo`) added to this file's own
  existing "guarded bare globals" list, matching its established pattern for every other
  `onclick=`-invoked function in this module (`dcFetchStrikes`, `_dcClose`, …) — this file's own
  header comment already documents why (inline handlers execute in global scope, not this file's
  closure).

## Verify

- `node --check` clean.
- **Playwright structural check** against the real, just-edited file: a tiny host page loading the
  actual `scorr_card_common.js` + `scorr_cockpit_card.js` from the repo, `/api/deriv/chain-grid/*`
  intercepted with a realistic synthetic payload built directly from `option_chain_grid.py`'s own
  documented shape (3 index strikes: one plain, one max-pain+call-wall, one put-wall).
  - 3 rows render with the right OI/CE-LTP/STRIKE/PE-LTP/OI header shape.
  - Row backgrounds: the plain strike has none; the max-pain+call-wall strike shows the gold
    max-pain colour (documented precedence); the put-wall strike shows teal — confirmed by reading
    each `<tr>`'s actual `style` attribute, not just eyeballing the screenshot.
  - Tapping the ATM row opens the detail panel with **both** legs' real numbers (premium, IV, IVP,
    both fair values, OI) plus the two honest "not tracked"/"not captured" lines; tapping the same
    row again closes it.
  - Tapping (i) shows the explainer with the expected copy ("max pain", "fair value" both present).
  - **A second scenario, a stock payload (`oi_available: false`)**: renders cleanly with OI columns
    as em-dashes, the legend's "index only" note, and zero wall/max-pain row colouring — confirming
    the most common real case (most D-button opens are stocks, not indices) degrades correctly, not
    just the index happy path.
  - Zero unexpected console/page errors across both scenarios.

## What this does NOT change

`strike_chain()` itself, `/api/deriv/strike-chain/{symbol}` (still there, unchanged, for any other
caller), cc#1859's tag computation, and every other section of the D-cockpit sheet (volume, OI,
futures basis, levels). Does not touch Index Intel (item 11's explicit out-of-scope).

## What still needs a founder look

A glass-check on a real device/screenshot (the card's own verify block asks for this) — table
shape, grid borders, tag dots, max-pain/wall highlighting, tap-to-detail, info toggle, legend, all
on real data. This session verified structurally with realistic synthetic data; it cannot substitute
for the founder's own visual sign-off. cc#2003 (Home popup) and cc#2006 (Market Mood capsule) can
now build on this same component — neither started this pass. Card not done — Fable verifies.
