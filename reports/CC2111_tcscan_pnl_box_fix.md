# cc#2111 — TC Scanner: oversized P&L box removed (bare `.v`/`.c` class collision) (P1)

## Root cause, confirmed by direct grep — and one more collision the spec asked me to check for
`mobile_app.css` (`mobile_endpoints.MOBILE_CSS`) defines a **bare, unscoped `.v{...}`** rule for an
entirely unrelated component — the Market Mood verdict card (`.v1`/`.v2`/`.vsym`/`.vside`/`.word`/
`.score`):
```css
.v{position:relative;background:var(--panel2);border:1px solid var(--line2);
  border-radius:15px;padding:15px 15px 12px 19px;overflow:hidden;margin-bottom:8px;cursor:pointer}
.v::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--vc,var(--amber))}
```
`mobile/tcscan.html`'s own `.scorr-prow .v` (cc#2105) only ever overrode **layout** properties
(`display:flex;align-items:baseline;gap:...;text-align:right;flex:none`) — never background, border,
radius, padding, position, margin or cursor. A more specific selector only wins on the properties it
actually declares, so all of that unrelated card chrome bled straight through onto TC Scanner's own
pct+amount pair: the oversized rounded, bordered, purple/panel2-tinted box the founder's screenshot
showed.

The spec asked me not to assume this was the only collision and to check for a similarly-bled-through
bare `.c` or `.n` class, since the founder's screenshot also showed **a nested white/light ribbon tag
around the rupee amount specifically**. Grepped `mobile_app.css` directly rather than assuming:
- **`.n`** — no bare rule exists anywhere in the file; every `.n` selector found is already scoped
  (`.chip .n`, `.tk .n`, `.bh .n`, etc.). Not affected.
- **`.c`** — a second bare, unscoped rule exists, for a *different* unrelated component (a generic
  list-card used elsewhere on the site):
  ```css
  .c{position:relative;background:var(--panel);border:1px solid var(--line);
    border-radius:14px;padding:13px 13px 13px 17px;margin-bottom:9px;overflow:hidden}
  .c::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2)}
  ```
  `positionRow()`'s real markup nests the rupee amount in exactly this class: `<div class="c">(...)
  </div>` inside `.v`. `.scorr-prow .v .c` (cc#2105) only sets `font-size`/`color`/`font-weight` —
  same story, same bleed-through — producing precisely the nested "white/light ribbon tag" the
  founder's screenshot showed, with its own 3px accent-bar `::before` too.

Both bare rules are correct and load-bearing for their own real components (Market Mood card, the
other list-card) — this fix does not touch either of them, only adds explicit resets scoped to TC
Scanner's own selectors.

## Fix
`mobile/tcscan.html`, `.scorr-prow .v` and `.scorr-prow .v .c` both gain an explicit reset —
`position:static;background:none;border:none;border-radius:0;padding:0;margin-bottom:0;
overflow:visible` — plus `.scorr-prow .v::before{display:none}` and `.scorr-prow .v .c::before{
display:none}` to kill both bled-through accent bars, and `cursor:default` on `.v` (the bare rule's
`cursor:pointer` was misleading on a non-interactive text span). `.n`'s rule, the `display:flex`
layout, and the pct/amount values themselves are untouched — only the container chrome is removed,
per the spec's own `do_not_touch`.

Removing the box's own padding (15px/12px vertical) and margin-bottom (8px on `.v`, 9px on the
nested `.c`) is also what satisfies item 3 ("shift content up to reclaim the space") — with the box
gone, the `.sub` line (cmp/timestamp + TC capsule), the price-rail bar, and the SL/TGT/exit-reason
tag all simply flow into the space the box's own box-model no longer claims. No separate
repositioning was needed or added.

## Verification

**Syntax**: both inline `<script>` blocks in the file re-validated with `node --check` (this was a
CSS-only change; confirming nothing else broke).

**Theme ratchets**, computed directly against `origin/main` content (`.html` is gated by both):
- Raw ratchet: `mobile/tcscan.html` 1→1, delta 0.
- Fallback ratchet: 0→0, delta 0.
- No baseline was on record for this file specifically (unmeasured, not a regression either way).

**`git diff --stat`**: only `mobile/tcscan.html` changed.

**Real-code Playwright harness**: assembled the full real cascade this page actually loads —
`scorr_themes.css`, `scorr_appshell.css`, `mobile_app.css` (extracted from `mobile_endpoints.py`,
the exact file whose bare rules caused this), the page's own inline `<style>`, and the real shared
`scorr_position_row.js` (verbatim, including its own self-injected CSS) — then called the real,
unmodified `positionRow()` extracted verbatim from the page against **two real closed TC Scanner
trades** (`tc_intraday_trades`, queried directly): YESBANK (LONG, +0.43%, +₹2,200/lot, real win) and
TCS (SHORT, −0.64%, −₹2,100/lot, real loss) — no OPEN position existed at test time (market closed),
stated honestly; closed rows exercise the identical `.v`/`.c` markup and CSS path `positionRow()`
uses regardless of open/closed state, so this does not weaken the check.

Built a **before** harness too, from `origin/main`'s pre-fix `tcscan.html`, to first confirm the bug
is genuinely reproducible before checking the fix (same discipline as cc#2106/cc#2107/cc#2110) — not
just assumed from the grep. 21/21 assertions pass:
- Before: `.v` computed background is a real colour (not transparent), real border, `border-radius:
  15px`; `.v .c` also carries a real background and `border-radius:14px` — the bug reproduces
  exactly as diagnosed.
- After: `.v` and `.v .c` both compute to `background:transparent;border:none;border-radius:0;
  padding:0;margin-bottom:0`; both `::before` accent bars compute to `display:none`; `.v`'s cursor
  is no longer `pointer`.
- Real text content preserved exactly: YESBANK renders `+0.43%` / `(+₹2,200/lot)`; TCS renders
  `−0.64%` (second row, sanity-checked).
- **Market Mood's own bare `.v` rule confirmed unaffected**: simulated a standalone `.v` element with
  no `.scorr-prow` ancestor (the real Market Mood card's actual DOM shape) in the same page/cascade
  and confirmed it *still* computes the full real card chrome (background, `border-radius:15px`,
  `padding:15px`) — the fix is provably scoped to TC Scanner's own selectors, the shared rule itself
  is untouched.

**Screenshots — looked at directly, per cc#2108's VISUAL_VERIFY_GATE_V1** (390×900, realistic phone
width):
- `cc2111_before.png`: both rows show the bug plainly — "+0.43%" and "−0.64%" each sit inside a
  large rounded, bordered, blue/panel2-tinted box, with a smaller dark rounded ribbon tag nested
  inside wrapping the rupee amount. The two cards together fill most of the 900px-tall viewport,
  leaving only empty page below.
- `cc2111_after.png`: same two real rows, same data — "+0.43% (+₹2,200/lot)" and "−0.64%
  (−₹2,100/lot)" now render as plain inline text beside the symbol row, no box, no border, no
  nested ribbon shape. Both cards are visibly, substantially shorter than before — the out-price/
  timestamp line, the ENTRY/CMP price rail, the SL/TGT labels and the SQUARE_OFF tag all sit
  noticeably higher, using the space the box used to occupy. Directly confirms both the box removal
  and the "shift content up" requirement in one look, not just in isolated CSS assertions.

## What did NOT change
The bare `.v`/`.c` rules in `mobile_app.css` and the Market Mood verdict card they serve (confirmed
above, both via `git diff` scope and a live DOM simulation). `.scorr-prow .v .n`'s own rule. The
pct/amount values and their one-line flex layout (cc#2105). `.cell .v` (the stat-row pill issue,
tracked separately in cc#2109, not duplicated here).
