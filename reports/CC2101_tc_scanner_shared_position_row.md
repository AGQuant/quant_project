# cc#2101 — TC Scanner unified onto V8's position-row design (shared component)

## Gate override (stated up front)
The card's own gate required cc#2098 AND cc#2100 to be shipped **and founder-confirmed** on
mobile/v8.html before this could be claimed. Founder instruction, direct, 15-Sep-2026: *"Fable not
available clear queue, push all to main Sonnet, nothing hold, re arm production mode on."* Both
cards are genuinely landed on main (dac69d8, ba4168e) with full verification of their own; only the
separate live-phone confirmation step is what's being waived here, per explicit founder instruction
— logged to `cc_task_logs` before claiming, per the Supersession process (rule 12).

## Ground truth first (item 2)
Grepped `tc_scanner_endpoints.py` and `scanners_app_mobile.py` before building anything:
- **No bucket/rule field exists on the row payload.** `tc_scanner_holds`'s own SELECT
  (`tc_scanner_endpoints.py:513-520`) never fetches `h.style`, even though the underlying table
  column exists — confirmed by reading the actual SQL, not inferred from the client. Per the
  card's own instruction, **no bucket filter was built.** Not invented, not silently skipped —
  stated here and in the task log.
- **`r.score` is on every row directly** (`mobile_tcscan()`'s own `rows()` helper,
  `scanners_app_mobile.py:73`), 0-100 scale already, confirmed by reading `_INSERT_HOLD_SQL`'s own
  comment ("score = the score100 at entry (rounded)"). No `mkTc()`-style lookup needed, exactly as
  the card predicted.
- **Exit-reason vocabulary**: grepped every `exit_reason=` write site in `tc_scanner_endpoints.py`
  — the engine only ever writes `OPEN`, `SL`, `TARGET`, `TIME`. Found an existing, already-shipped
  mapping for this exact vocabulary in `v8_dashboard.html`'s `reasonPill()` (the web TC Scanner
  tab, reading the same table) — reused that classification (TARGET→win, TIME→"Time stop", else→
  loss) rather than inventing a new one.

## Shared module: `scorr_position_row.js` (new, item 1)
Site-wide via `_MOBILE_HEAD` (same pattern as `scorr_card_strip.js`: read-once-at-import in
`pwa_endpoints.py`, own route, `defer` script tag). Exports `window.ScorrPositionRow` with the
**genuinely page-agnostic** pieces only:
- `trkAxis`/`trkFrac`/`trkTickStyle`/`trkFixLabels` — the price-rail geometry (cc#1660/cc#1726),
  pure number math plus one generic DOM pass with no page-specific selectors.
- `fnTcBand` — the TC_CANON_V2_FINAL band thresholds (cc#2099), pure number-in/string-out.
- `fnTcCapsule` — a generic capsule renderer for a page with no prior version (TC Scanner uses it
  directly; see below for why V8 does not).
- `cardStripToggle` — the C·A·R·D tap-reveal toggle (cc#2098), pure DOM, zero page coupling.

**What is deliberately NOT shared, and why** (item 1's own "CC's call"): the actual row markup
(symbol, P&L headline, meta line) and `trkLabels`'s text assembly stay local to each page. V8 has
a basket badge and leads with rupees; TC Scanner has no basket and leads with percent; the two
engines' field names differ (`stop_loss`/`entry_price` vs `sl`/`entry`). Forcing these into one
function would only move the per-engine branching into a parameter object, not remove it.

**A real mistake caught before shipping**: the module's CSS originally used
`var(--token, #hexfallback)` for every colour (defensive habit) — 17 hits on the fallback ratchet,
which explicitly scans `.js` files. Since every token used (`--hi`/`--edge`/`--win`/`--loss`/
`--muted`/`--brand`) is in the official `scorr_themes.css` contract that both pages already link,
the fallbacks were pure redundant risk. Rewritten with bare `var(--token)` — verified 0.

**V8's `fnTcCapsule` was deliberately NOT delegated to the shared module.** Its no-score dash state
uses `var(--chalk)`/`var(--mono)` — v8.html's own local token aliases, matching the original GVM
column's em-dash exactly (cc#2099's own deliberate choice). The shared module's generic version
uses the portable `--muted` token instead, which is *not* the same colour reference. Rather than
silently change V8's dash colour, `fnTcCapsule` stays local to v8.html (calling the now-shared
`fnTcBand` internally) — a real, considered scoping decision, not an oversight.

## mobile/v8.html: refactor only, verified byte-identical output (item 1's verify clause)
`trkAxis`/`trkFrac`/`trkTickStyle`/`trkFixLabels`/`fnTcBand`/`cardStripToggle` are now one-line
wrappers delegating to `window.ScorrPositionRow`; `trkLabels`/`fnTcCapsule`/`posRow`/`closedRow`
are untouched. Kept as normal `function` declarations (not `var`-aliased at script-parse time) so
they are safe regardless of script-tag load order — the shared module has always finished loading
by the time any of these are actually *called* (`posRow` only runs after `load()`'s fetch
resolves, long after page parse completes), verified rather than assumed.

**Verification**: a direct before/after render, real data, same 6 real open positions used in
cc#2100's own test (`v8_paper_positions` × `cmp_prices`) — the pre-refactor function bodies
(captured during cc#2100's own verification) vs the post-refactor thin wrappers, run through a
real Chromium page including the `trkFixLabels` collision pass. **Byte-identical on both the
per-row HTML string and the full post-collision-pass DOM, all 6 rows.**

## mobile/tcscan.html: rewritten onto the shared component (items 3, 4, 6, 7)
- Row is no longer a bare `<a href="/m/check?sym=...#tc">` link — it now matches V8's shape:
  symbol+side badge / P&L headline / meta line / **new** SL-entry-CMP-target rail (TC Scanner had
  no bar before this card) / TC capsule + exit-reason badge / tap-reveal C·A·R·D strip. This is a
  genuine behaviour change (the whole-row navigation shortcut is gone), traded for a richer set of
  destinations through the strip (Chart/Analysis/Result/Cockpit vs. the single Check link) —
  flagged here plainly, not slipped in silently.
- Item 4 preserved exactly: `pnl_pct` stays the headline number, `pnl_rs`/lot stays secondary —
  unchanged convention from before this card.
- Item 5: `tcTagRes()` reuses the `.tag.res.{t|s|g}` pill *shape* V8's `resTag()` uses, with TC
  Scanner's own real word list (TARGET/TIME→"Time stop"/SL) and its own tokens (`--win`/`--loss`/
  `--brand` — the page already uses these directly, unlike v8.html's local alias layer).
- Item 3's marker/flag slot: TC Scanner has no equivalent signal source to V8's `mkFired`/`mkKeys`
  — that slot is simply not rendered, per the card's own explicit allowance, never fabricated.
- Item 6: added a sort control (P&L ↓ default / P&L ↑ / Newest / Oldest) for the Open list, which
  was previously always P&L-desc with no user choice.
- Dead code removed: `f1()` (the old inline "TC n.n ·" text formatter) had no remaining call sites
  once the score moved into the capsule.

## Verification
- `node --check` clean on all three touched/new JS payloads (`scorr_position_row.js`,
  `mobile/v8.html`, `mobile/tcscan.html`); `ast.parse`/`py_compile` clean on `pwa_endpoints.py` and
  `main.py`.
- **Raw-primitive + fallback ratchets**, computed directly against `origin/main`: `mobile/v8.html`
  0/0 delta, `mobile/tcscan.html` 0/0 delta. `scorr_position_row.js` itself: 0 fallbacks (a brand
  new file is held to zero on that ratchet); its raw-primitive count is informational only per
  `theme_validator`'s own rule that a new file is "unmeasured," not gated, on first appearance.
- **grep-confirmed** (the spec's own verify list): zero duplicate `trkAxis`/`trkFrac`/
  `trkTickStyle`/`trkFixLabels`/`fnTcBand` definitions in `mobile/tcscan.html` — all reference
  `window.ScorrPositionRow`. `mobile/v8.html`'s own copies are one-line wrappers only.
- **Real-browser tests (Playwright), real production data**:
  - V8 byte-identical-output regression (above), 7/7 checks pass.
  - TC Scanner: 11 real rows (`tc_scanner_holds` × `cmp_prices`, today's open book + this week's
    closed book, symbols ICICIGI/360ONE/MFSL/ASHOKLEY/RELIANCE/VEDL/AMBUJACEM/BRITANNIA/KPITTECH/
    GRASIM/JINDALSTEL) plus exactly one clearly-labelled constructed row (an SL exit — no real SL
    example existed in this sample) run through the actual committed `positionRow`/`tcTrkLabels`/
    `tcTagRes`/`sortOpen`, extracted verbatim (brace-matched) from the committed file — **33/33
    assertions pass**, including:
    - All 5 real open rows correctly band STRONG (real scores 84-89 ≥ 84).
    - Real TARGET win → `t`/green badge; real TIME exit → `g`/neutral badge, "Time stop" text;
      a real TIME exit that was actually a **loss** (GRASIM, -1.44%) renders the negative colour
      class correctly; constructed SL → `s`/red badge.
    - Real C·A·R·D tap-reveal (real `scorr_card_strip.js` loaded): opens, closes, and the strip's
      own "A" pill click does not re-collapse the row — same three checks cc#2098 proved for V8.
    - Sort correctness against real `entry_ts`/`pnl_pct` values, including a genuine JS-stable-sort
      edge case (two real pairs share an identical `entry_ts` to the second) — verified the
      correct tie-break behaviour rather than assuming a naive "exact reverse" that ties make
      untrue.

## What did NOT change
`TC_CANON_V2_FINAL`/the TC scorer — read-only. `scorr_card_strip.js` — consumed, not modified.
V8's `posRow`/`closedRow`/`trkLabels`/`fnTcCapsule` bodies, and every other surface on
`mobile/v8.html` — byte-identical, verified above. TC Scanner's hero/KPI cells, side segment, date
chips, rule card — untouched.

## Live checks still needed (Arpit)
Rail renders and reads correctly on a real phone for both pages. TC Scanner's sort control
actually reorders the list on tap. The C·A·R·D strip opens correctly on TC Scanner rows, D
disabled/enabled correctly per futures-only (shared component's own existing gate, unchanged).
Confirm the loss of the direct "tap row → Check page" shortcut on TC Scanner is acceptable, given
the richer C·A·R·D replacement.
