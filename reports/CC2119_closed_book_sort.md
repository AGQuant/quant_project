# cc#2119 — TC Scanner Closed Book: click-to-sort headers via the shared `sortTbl()`

## What changed (`v8_dashboard.html` only, inside the TC13 IIFE + its CSS block)
1. **`TC13_CLOSED_COLS`** beside `TC13_OPEN_COLS`: the same eight columns the hardcoded `<thead>`
   rendered, same labels — `Symbol · Side · Entry · Exit · Result · P&L% · P&L ₹ (1 lot) · Closed
   Date` (cc#2077 wording kept); the P&L ₹ header keeps its lot-rule `title`, carried on a span
   inside the th because `sortTbl` builds the th itself.
2. **`tc13ClosedRow(r)`** — pure extraction of the former row closure; byte-identical cells
   (`.sym`, `.side buy/sell`, `reasonPill`, `cl2/pnlTxt/rsTxt/dShort`, the lot-size title).
3. **`closedTable()`** now calls `sortTbl('tc13closed', TC13_CLOSED_COLS, closed, tc13ClosedRow,
   {sortKey, dir, empty, wrapClass:''})` inside the same `.tc13-card` wrapper. **Sort is carried
   across re-renders** exactly as the Open Book does it (`_prevCSort = SORTREG['tc13closed']`
   before the call), so a side-filter tap or a date-range refetch never resets it.
   **Default `sortKey: null`** — the initial order is the endpoint's own order, unchanged from the
   hand-built table (verified: BUY rows then SELL rows, each exit-desc, same as before). The empty
   sentence (side-aware, with the last-closures suffix) goes through `opts.empty`, never the
   component's generic "No rows".
4. **CSS trap handled — option (a), the page's own precedent.** cc#1884 styled the Open Book's
   sortTbl table by wrapper id (`#tw_tc13open …`). The `.tc13-wrap .tbl` rules (table, th, the
   first-two-columns-left rule, td, the cc#2074 `tbody tr{background:var(--panel)}` row surface,
   last-row border, hover, `.sym`) are now `#tw_tc13closed …` rules with the same values, and the
   `#tw_tc13open .scroll…` / `.empty` rules gained `#tw_tc13closed` as a second selector. Nothing
   else on the page used `.tbl` (grepped). `var(--panel)` stays a token — literal white in light,
   navy in dark, no hardcoded literal (0 new fallbacks).
5. **`tc13MarkScroll`** (TC13's own measurer, not the shared sorter) now also covers
   `#tw_tc13closed .scroll`, so the SCROLL → chip and the fade edge keep working on a phone.

**Shared sorter untouched**: `sortTbl / drawTbl / tblSort / SORTREG` — 0 changed lines (checked
in the diff). `closedKpis()`, `bookRs()`, the capsules, `fRows`, the date controls, the Open Book's
call, `TC13_OPEN_COLS`, `tc13OpenRow()`, `reasonPill/cl2/pnlTxt/rsTxt/dShort/n2` — untouched.

## Verified on the REAL page (Playwright, 152 checks, all pass)
The real `v8_dashboard.html` served locally with the real `scorr_web_tokens.css` injected the way
`main.py` does, the TC Scanner pane booted through its own `tc13Boot()`, `/api/scanners/tc/holds`
answered with the **32 real closed rows** (7 BUY / 25 SELL, `tc_scanner_holds` × `futures_universe`
× the endpoint's own `pnl_pct`/`pnl_rs` formulas, `exit_ts` in the endpoint's `str(datetime)`
format). Dark and light, 1280px and 375px:
- Eight headers, labels unchanged, ⇅ on every header before any click, no caret.
- **Every header sorts, both directions**: each of the 8 columns clicked twice; the rendered row
  sequence equals an independent Python sort of the real rows with `drawTbl`'s own rules (strings
  by string order, numbers numerically, stable) — asc on first click (cc#262), desc on the repeat,
  caret ▲/▼ on that column only. 16 of 16 per context.
- **Result** sorts on the raw `exit_reason`: the 8 SL rows are contiguous, then TARGET, then TIME
  (Time stop) — not on the pill markup.
- **Closed Date** sorts chronologically: the string order of the real rows (07-Sep → 16-Sep,
  mixed with/without microseconds) equals the datetime order both ways. Caveat stated: the real
  book spans ten days, not two months — the format is fixed-width to the seconds
  (`YYYY-MM-DD HH:MM:SS[.ffffff]`), which is what makes a string compare chronological across any
  span; no `_daysN`-style twin was needed.
- **P&L ₹ nulls**: the real book has 0 rows without a lot (the Realised capsule shows no "without
  lot" figure — consistent). `drawTbl` maps null to −1e9 so such rows would sink to one end; not
  exercisable on real rows today, said rather than claimed.
- **Sort → side filter (cc#2118 has landed, fa1e63d)**: P&L% asc, tap Short → `SORTREG` keeps
  `pnl_pct / +1`, the 25 SELL rows come out in that order, caret still on P&L%, capsules narrow to
  Short (Closed 25). Back to All restores 32.
- **Sort → From/To change** (refetch + full re-render): Closed Date asc survives, caret shown, rows
  re-sorted.
- **Capsule invariant**: Realised +₹1,08,695 / Accuracy 62.5% (20 of 32) / Avg Profit +₹3,397 are
  byte-identical before and after 16 sort clicks.
- **Horizontal scroll** (375px, table overflows): scrollLeft 40 → sort click via the header's own
  onclick → 40 (cc#1552 applies). As rendered, the sortTbl `.scroll` carries `clipped` and the
  SCROLL → chip is on (tc13MarkScroll covers the Closed Book).
- **Styling parity**, computed: th 9.5px uppercase `10px 14px`; first two columns left-aligned,
  numeric right; td `11px 14px`; table 13.5px; `.sym` 700; **tbody row background == this theme's
  `--panel`** (dark `#121A33`, light `#FFFFFF`); hover == `--raise`.
- VISUAL_VERIFY_GATE_V1: four screenshots looked at (dark/light × 1280/375), each in a sorted
  state with the caret visible.
- `node --check` clean on all 8 inline script blocks; 0 new `var(--x, #literal)` fallbacks.

## Not done / seen
- The one page error during the harness is from another pane's stubbed metrics endpoint
  (`cache.metricsAll.reduce`), not TC13.
- Pre-existing, same on the Open Book: the fade-edge `clipped` class lives on the `.scroll` div
  that `tblSort` replaces on every sort click, so the fade is lost after a sort until the next
  render/resize (the SCROLL → chip in the header stays). Fixing it means touching `tblSort` or
  re-measuring after every sort — outside this card; named here.
