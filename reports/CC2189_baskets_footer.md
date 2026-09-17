# cc#2189 — /m/qb baskets landing: the card footer is two clean labelled stats

Built 17-Sep-2026 (founder 14:58 IST); the landing time is in the task row. Page only: `mobile/qb.html` (the `spine()` card and its CSS). Data, returns, marked timestamp and the card header are untouched.

## Diagnosis (in the file, before editing)

The footer row was `<div class="row"><span class="k">…</span><span class="v">…</span>` — `.row`, `.k` and `.v` are also bare selectors in the shared mobile_app.css: `.v` is a boxed value pill with a `::before` left bar and its own height. That is the founder's picture: the caption half-hidden behind the amount pill, the thick left bar on the date pill, the pills crossing the divider. Same family as cc#2155 / cc#2166.

## What changed

- The footer is its own `qb-` prefixed set: `.qb-foot` (a grid row of its own, spanning the name column and the sparkline column so the two captions get the card's full width), `.qb-k` caption above `.qb-v` value. No pill outline, no bar; the value keeps the card's bold value style.
- Stat 1: MIN · ONE SHARE EACH / ₹27,409. Stat 2: NEXT REBALANCE / 05 Oct · monthly (the frequency word rides with the date so nothing the card said is lost); a basket without a next date shows REBALANCE / monthly.
- One `--edge` hairline above the footer with a 12 px gap on both sides (`margin-top` and `padding-top` both `--space-12`); the card's own left rail now spans both grid rows.
- One `spine()` renders the CURATED and QUANT sections alike, so both get it. Tokens only; no new literal.

## Harness (Playwright, real Chromium, `scratchpad/cc2189_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture: 7 cards in the endpoint's shape (1 curated + 6 quant, one with no next date and no history).

- 7 cards, two labelled stats each; every caption sits above its value; no caption or value clipped (checked per element, `scrollWidth` vs `clientWidth`).
- No `::before` bar and no left border on any value (the leak is gone).
- One 1 px hairline with 12 px above and below; the footer sits inside the card.
- Model Portfolio → ₹27,409 / NEXT REBALANCE 05 Oct · monthly; Small Cap → 01 Oct · quarterly; the no-date card → REBALANCE monthly.
- The card's left rail spans the card (both rows). No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2189_*.png`): `375_light_cards` (full page) — CURATED with Model Portfolio, then the six QUANT cards: name and one-line description, the return and "since … · Nifty" on the right, a hairline, then MIN · ONE SHARE EACH with the amount and NEXT REBALANCE with "06 Oct · monthly" side by side, fully readable, nothing crossing the line; Contra Value shows REBALANCE / monthly and "no history yet". `375_dark_cards` — the same on Gold Night.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/qb` on the phone, light theme: every card's footer reads as two captions with their values, no clipped caption, no bar on the date.
