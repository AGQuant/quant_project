# cc#2115 — DMA_CROSS_WINDOW_1M: third marker family alongside DMA_STATE_V1 (P2)

## What this adds
A new GREEN/RED marker: has the 5DMA crossed the 20DMA at any point in the last 21 trading
sessions, and which way, most recently. The existing state square (DMA_STATE_V1, cc#1682) says
"is 5DMA above/below 20DMA today" — always defined, every open symbol has one. This new marker
answers a different question — "did that relationship just FLIP, and when" — which tells a reader
which of today's red squares are freshly turned versus been-red-for-weeks (the card's own
evidence: 1 green / 12 red on the open book, indistinguishable by freshness before this).
Genuinely absent for most symbols on most days — confirmed on real data below — never guessed.

## Backend (`v8_pivot_star.py`)
- `evaluate_dma_cross_window(conn, window_days=21, target_date=None)`, modeled on
  `evaluate_dma_cross()`'s own single-day flip test (`sma5_t>sma20_t and sma5_y<=sma20_y` for a
  bullish flip, mirrored for bearish) — repeated across session offsets `t=0..window_days-1`
  (0 = the latest completed close), stopping at the FIRST match, which is therefore the MOST
  RECENT flip. `window_days=21` is trading **sessions**, not a calendar month — sidesteps
  weekend/holiday roll logic entirely. This is the one half of the founder's ask that wasn't
  explicitly confirmed (calendar-month-with-roll vs. sessions) — flagging it back here rather than
  guessing, per the card's own assumptions section.
- Insufficient history (`<41` closes = `window_days(21) + DMA_SLOW(20)`) skips the symbol, same
  "never a guessed cross" rule the sibling functions use. Fetch pulls ~45 closes (21+20+4-session
  buffer), sized off the actual `window_days` argument, not a hardcoded default.
- `_fetch_dma_window_support()` is a **separate** function from `_fetch_dma_state_support()` (not
  a parameterised reuse) — that helper backs the two DO_NOT_TOUCH state functions and must not
  risk a behaviour change to either. `_score_dma_cross_window()` is the pure flip-scan math.
- Same open-book candidate query as `evaluate_dma_state()` (open positions, retired baskets
  excluded, same era cutoff).
- **Write side** (`run_tick()`): a new block after the existing DMA-state write, same shape
  (`data_date`-keyed `star_date`, `ON CONFLICT (symbol, star_date, direction) DO NOTHING`
  first-fire-per-day). `touched_dates` (the `date[]` column channel_reject already reuses for its
  own single touch-date) carries the flip's own date — column reuse, stated, no `ALTER TABLE`
  (MAINTENANCE_LOCK_RULE cc#351). A tick with zero qualifying symbols is a valid, explicitly-logged
  empty result (ENGINE_LIVENESS_RULE), not an error.
- **Read side** (`/api/v8/pivot_star`): a sixth list, `dma_cross_1m` — own key, own map (cc#933's
  rule: a symbol can carry the state square and this marker at once). Same LOG-read/
  DISPLAY_PARITY doctrine and the same `DISTINCT ON (symbol) ... ORDER BY star_date DESC` shape
  `dma_state` uses, for the identical reason: this marker's own `data_date` legitimately lags the
  resolved date during market hours (today's `raw_prices` row is a partial candle, excluded).
  **Design decision, stated plainly**: unlike `dma_state` (defined every day, so "latest row ever"
  is always fresh), this marker is genuinely absent on many symbol-days. A naive latest-ever read
  could resurrect a flip that has since aged out of its 21-session window if the scheduler ever
  stalled. Added a 4-calendar-day staleness bound on the read (`star_date >= resolved_date -
  INTERVAL '4 days'`) — comfortably covers one normal weekend plus a Monday holiday, far short of
  the 21-session window itself, without requiring a live re-evaluation here (which DISPLAY_PARITY
  forbids). This is a documented judgement call, not asked for explicitly in the card's own verify
  section — the alternative (no bound) risks a stale marker lingering indefinitely under a stalled
  scheduler; this one risks a marker disappearing a few days later than mathematically exact in
  that same failure case. Neither risk touches a real number or a live badge running ahead of data
  — this is a "recently crossed" convenience flag, not a P&L figure.
- Legend line added: "Up/down arrow = 5DMA crossed 20DMA within the last month, most recent flip."

## Web dashboard (`v8_dashboard.html`)
`PIVOT_DMA1M` map (own map, cc#933 rule), populated from `d.dma_cross_1m` in `loadPivotStars()`.
Wired into `markerFiredFor()`/`markerKeysFor()`/`_mkCounts` the same way `dma` already is. Two new
mchip quick-filter badges — `5M>20` (green, ↑) and `5M<20` (red, ↓) — an unused glyph pair per the
card's own instruction (star/bolt/square/triangle are all already spoken for). `scorr_card_common.js`'s
`ScorrMarkerFlagFilter` gets a `dma1m: fired.dma1m || null` pass-through (unfiltered by side, same
reasoning as `dma`: "did 5DMA cross 20DMA" is true/false independent of which way the position is
held) — a minimal, additive line; the glyph-priority/detail-popover functions (`ScorrMarkerFlagColor`/
`ScorrMarkerFlagDetailHtml`) are untouched, out of this card's scope (the mchip filter row, not the
single consolidated symbol-column glyph — same precedent `chan`/channel_reject already set: included
in the shared `fired` object for consistency, no detail-popover row).

## Mobile app (`mobile/v8.html`, not `mobile_endpoints.py`)
Investigated both mobile surfaces before touching either:
- **`mobile_endpoints.py` ~line 451** (the card's own citation): `mobile_v8_positions()`'s star
  query — `SELECT symbol, star_color FROM v8_pivot_star_log WHERE star_date = today` — has **no
  `direction` filter at all**. Every family, including `DMA_ABOVE`/`DMA_BELOW` today, already
  collapses into one generic per-symbol `star_color` bucket here. Once this card's write-side
  change lands, `DMA_CROSS_UP_1M`/`DOWN_1M` rows flow through this exact same query automatically —
  "mirrors... the same way the existing state squares do" literally, with zero code change needed.
  Verified by reading the query directly, not assumed — confirmed no other file reads this
  endpoint with a `direction` filter either (repo-wide grep). **No change made here.**
- **`mobile/v8.html`** (the actual richer mobile V8 page, cc#1610): fetches `/api/v8/pivot_star`
  directly and maintains its own parallel `MK.dma`/`mkFired()`/`mkKeys()`/`MK_CHIP_DEFS`/
  `renderMarkerChips()` — an exact structural twin of the web dashboard's marker system. This is
  where the state squares genuinely get their rich, distinguishable treatment on the mobile app,
  and it does NOT automatically pick up a new endpoint list the way the generic field above does.
  Wired `MK.dma1m` through the identical five points (`MK` init, fetch/populate, `mkFired`,
  `mkKeys`, `MK_CHIP_DEFS` + `renderMarkerChips`'s counts/chips) mirroring the web dashboard change
  exactly. `mkFiredChips()` (the per-row inline chip renderer) needed no change — it already reads
  generically off `MK_CHIP_DEFS`/`mkKeys()`, so the two new keys flow through automatically.

## Verification

**Syntax**: `ast.parse`/`py_compile` clean on `v8_pivot_star.py`. `node --check` clean on
`scorr_card_common.js` and every inline `<script>` block in `v8_dashboard.html` (8) and
`mobile/v8.html` (2).

**`do_not_touch` proven, not asserted**: extracted `evaluate_dma_cross()`, `evaluate_dma_state()`,
`evaluate_dma_state_universe()`, `run_dma_state_eod()`, `_fetch_dma_state_support()`,
`_score_dma_state()` from both `origin/main` and the working tree and diffed them — all six
byte-identical, confirmed programmatically.

**Theme ratchets**, computed directly against `origin/main`: fallback (`.py`/`.js`/`.html` all in
scope) delta 0 on all four files; raw ratchet (`.html` only) delta 0 on `v8_dashboard.html` and
`mobile/v8.html`.

**`git diff --stat`**: exactly the four intended files.

**Real production data, not synthetic**: the current open book is 13 positions (matches the card's
own evidence exactly). Wrote an independent SQL re-implementation of the flip-scan (window
functions over real `raw_prices` closes) against all 13 open symbols — 10 had a real flip inside
21 sessions, 3 (CAMS/HDFCBANK/SBILIFE) correctly had none despite 1,300+ closes of history each
(ample data, genuinely no qualifying flip — the "absent is real" case, not a data gap). Then
**hand-verified 3 symbols' exact arithmetic** against raw closes pulled directly from the DB:
- ADANIPORTS: bullish flip 4 sessions ago (08-Sep) — computed sma5=1688.80, sma20=1683.37 by hand,
  matching the SQL replication exactly.
- 360ONE: bearish flip 6 sessions ago (03-Sep) — sma5=1175.38, sma20=1181.64, exact match.
- BHARTIARTL: bearish flip 16 sessions ago (21-Aug) — sma5=1942.64, sma20=1948.01, exact match.

All three match to the cent, confirming the Python formula (identical arithmetic to what was
hand-computed) is correct — this satisfies the card's own "spot-check 2-3 by hand against
raw_prices" verify item.

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1** (this card touches
rendering on two surfaces, unlike a pure backend change):
- Web dashboard: built a harness using the real `mchip()` function extracted verbatim, real
  `scorr_themes.css` tokens, and real counts (tcs/s1/r1/act/dmaUp/dmaDn from `v8_pivot_star_log`;
  dma1mUp=1/dma1mDn=9 from the hand-verified real flip-scan above). All 8 chips render cleanly in
  one row — correct glyphs, correct colours (green ↑ / red ↓ for the two new ones, clearly
  distinct from the existing green/red ■ state chips), correct counts, no overlap or clipping.
- Mobile app: same approach against `mobile/v8.html`'s own `.chip`/`#open-markers` markup and CSS.
  First attempt used an incomplete hand-picked CSS subset and rendered wrapped/broken text —
  caught by actually looking at the screenshot (exactly the failure mode cc#2108 exists to catch),
  traced to a missing `#v8p .filters .chip{flex:none}` rule, fixed by re-extracting every
  `.chip`/`.filters` rule from the real file by line number instead of hand-picking. Re-screenshot
  confirmed clean, compact pills — all 8 chips readable, correctly coloured and counted, inside the
  same `overflow-x:auto` scrollable row the basket/filter chips already use (cc#2107's own
  territory) — structurally immune to overflow regardless of chip count.

## What did NOT change
`evaluate_dma_cross()`, `evaluate_dma_state()`, `evaluate_dma_state_universe()`,
`run_dma_state_eod()` and their shared helpers — all six confirmed byte-identical. The DMA_ABOVE/
DMA_BELOW state squares' own behaviour, colours, and glyph. `mobile_endpoints.py` (verified,
not changed — see above). `ScorrMarkerFlagColor`/`ScorrMarkerFlagDetailHtml` (glyph-priority and
detail-popover — out of this card's scope). `mkFiredChips()` in `mobile/v8.html` (already generic,
needed no edit).
