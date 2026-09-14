# cc#2053 — Home Approved Trades detail sheet: de-duplicate Target/Stop, restructure Entry/Approved

Founder ask (dictated this session, on the ADANIGREEN · Long detail sheet), amended in a follow-up:
Target and Stop Loss each printed twice. Amendment: the later Target row is **relabelled**, not
deleted — "Potential left", value + % only, no price repeated. The later Stop row is still removed
outright (no amendment covered it). Also: Entry/Approved should separate date from price instead
of a run-on string.

## Step 1 gate — re-confirmed before editing

`apTrDetailOpen(p)` matched the card's own cited lines exactly: the bare Target/Stop Loss pair
right after Tag, the richer Target row after Net P&L (`'level · value left · % move needed'`), and
the bare Stop row (`'level only'`) after it. Confirmed via direct read, not assumed.

## The fix

- **Target (bare row, after Tag): unchanged** — the one place the price prints.
- **Target (richer row, after Net P&L): relabelled to "Potential left"**, sub-label trimmed to
  `'value left · % move needed'` (drops "level ·" since no level/price is shown here anymore),
  value is `apTrPnlHtml(p.reward_left, p.reward_left_pct)` only — the same value/percent helper Day
  P&L and Net P&L already use, not a new formatter. No-live-price state falls back to `'—'`,
  matching how every other P&L-derived row on this sheet already handles a missing `cmp`.
- **Stop (richer row, `'level only'`): deleted outright** — it carried no value beyond a bare
  repeat of the price already shown above, and the founder's amendment was specifically about
  Target, not Stop.
- **Entry / Approved (and the manual-alert single-Entry variant): three-part layout.** New
  `apTrDetailRowDate(k, sub, dateStr, priceStr)` renders label · date · price as three sibling
  `<div>`s instead of squeezing a timestamp and a rupee figure into one string in `apTrDetailRow`'s
  single value column. `.apd-row`'s own CSS (`justify-content:space-between`) already spaces three
  children correctly with no change needed; the price re-uses the existing `.apd-v` class verbatim
  so it keeps the exact same bold weight every other value on the sheet has — new `.apd-date` is
  deliberately less prominent (smaller, muted), so the price still reads as the important number.
  Every other row (Tag, Target, Stop Loss, Day P&L, Net P&L, Potential left) stays on the original
  `apTrDetailRow`, confirmed unchanged by test.

**Do not touch, respected**: the carousel cards themselves (`apTrCardHtml`) — untouched, only the
tap-to-open detail sheet was edited. Day P&L / Net P&L rows and their wording — byte-identical.
No backend change — `reward_left`/`reward_left_pct` are read exactly as before, only relabelled.

## Verify

`node --check` clean. Real headless Chromium, `apTrDetailOpen()` and its full dependency chain
(`apTrDetailRow`, the new `apTrDetailRowDate`, `apTrPnlHtml`, `apTrPx`, `esc`, `inr`, and the other
`apTr*` formatters) **extracted verbatim from the committed file** (self-checked for brace balance
before running — caught and fixed one off-by-one line-slice, `apTrPnlHtml`'s own closing brace, before
it could produce a false result) — **20/20 checks pass**, across three fixtures (a V8 trade with a
live price, the same trade with no live price, and a manual-alert/non-V8 trade):

- Target's price now appears **exactly once**; Stop Loss's price now appears **exactly once**.
- "Potential left" is present, carries the real `reward_left`/`reward_left_pct` values, and — read
  directly, not assumed — **contains no target price number anywhere in that row**.
- The old bare "Stop" label is gone entirely; "Target" now appears exactly once (the bare-price row).
- Entry and Approved both render through the new three-part layout with the correct price in each;
  the old single concatenated "date · price" string pattern is confirmed gone. Exactly 2 rows use
  the new layout, exactly 6 keep the old two-column shape (Tag, Target, Stop Loss, Day P&L, Net
  P&L, Potential left) — the do_not_touch boundary holds precisely, not just by intent.
- No-live-price state: Potential left correctly falls back to a dash rather than blank/undefined;
  Target/Stop Loss prices are unaffected (they never depended on `cmp`).
- Manual-alert (non-V8) trade: exactly one Entry row (the single-Entry variant), still three-part,
  with no separate Approved row rendered.
- Zero page errors across every fixture.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): opening the
live ADANIGREEN · Long sheet on-glass to confirm it reads cleaner, per the card's own verify
section.
