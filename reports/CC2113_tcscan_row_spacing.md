# cc#2113 — TC Scanner: row spacing increased after cc#2111 box removal (P2)

## Why
Direct founder follow-up right after confirming cc#2111 (the oversized P&L box removal) shipped
correctly: *"yes, it is shipped, but I want the height increased a bit, card height is a little
less, it should be increased so content becomes spacious."* Expected — `.scorr-prow`'s own padding
and `.sub`'s margin-top were tuned for the OLD layout, where the (now-removed) `.v` box's own
`padding:15px 15px 12px 19px` and `margin-bottom:8px` contributed real vertical height on top of
the row's own spacing. With that box's box-model gone (cc#2111's whole point), the row's own
spacing alone reads as tight.

## Fix
Two values, both in `mobile/tcscan.html`, both already on the app's existing `--space-N` (=Npx)
token scale — no new tokens introduced:
- `.scorr-prow` padding: `var(--space-10) var(--space-12)` → `var(--space-14) var(--space-12)`
  (vertical 10px → 14px top/bottom; horizontal unchanged).
- `.scorr-prow .sub` margin-top: `var(--space-2)` → `var(--space-6)` (2px → 6px, the gap between
  the header row and the cmp/timestamp line).

`positionRow()` is the one shared function building both the Open and Closed lists (confirmed by
reading `draw()` directly — both `openRows.map(positionRow)` and `closedRows.map(positionRow)` call
it), and both rules are plain CSS selectors with no list-specific scoping, so this one change
applies identically and automatically to every row in both lists — not two separate edits.

Left untouched, per the card's own scope: the price-rail (`.trk`) bar's own `margin-top:18px`/
`margin-bottom:16px` — that spacing lives in the shared `scorr_position_row.js` (used by both TC
Scanner and V8), and this card is explicitly scoped to TC Scanner only.

## Verification
- Theme ratchets computed directly against `origin/main`: raw 1→1, fallback 0→0, both delta 0.
- `git diff --stat`: only `mobile/tcscan.html` changed (6 insertions/2 deletions — a comment plus
  the two value changes).
- **Real-code Playwright harness**, reusing cc#2111's real-cascade generator (same real
  `mobile_app.css`/`scorr_themes.css`/`scorr_appshell.css`/`scorr_position_row.js`/`positionRow()`,
  same two real closed trades, YESBANK win + TCS loss) regenerated against the now-current file.
  10/10 assertions pass: row `padding-top`/`padding-bottom` now compute to `14px` (both the first
  AND the second row — confirms the shared-function, both-lists claim); `.sub`'s `margin-top`
  computes to `6px`; row height grew from the cc#2111 baseline (132px vs. ~120-125px); and — the
  card's own explicit `do_not_touch` — `.scorr-prow .v`/`.v .c` still compute zero background,
  border, radius and padding, confirming cc#2111's chrome removal was not disturbed.
- **Screenshot — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1** (390×900, same real
  YESBANK/TCS rows as cc#2111's own screenshots, so this is a true apples-to-apples before/after):
  against `cc2111_after.png` (the cc#2111-shipped, pre-cc#2113 state already on file), the new
  `cc2113_after.png` shows visibly more breathing room — more space above "YESBANK" at the card
  top, a clearer gap between the header row and the "out 25.52 · 18 Jun 15:15" line, and more room
  before the price rail starts. Both cards together now run to about the same proportionally taller
  footprint (+~25px combined, matching the +12px/row × 2 rows the CSS change computes to). The card
  reads as comfortably spaced rather than cramped — and the pct+amount pair is still plain text,
  no box, no border, no ribbon shape reintroduced anywhere.

## What did NOT change
`.scorr-prow .v`/`.v .c` and their `::before` resets (cc#2111, confirmed still in effect above).
The price-rail bar's own spacing (shared file, out of scope). Every other page/card's spacing (V8,
GVM, etc. — untouched, per this card's own scope).
