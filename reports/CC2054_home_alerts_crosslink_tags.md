# cc#2054 — cross-link Home EXPERT CURATED and /m/alerts TRADE ALERTS

Founder ask (two screenshots, same symbols LTM/ADANIGREEN on both surfaces): show a small tag
beside each header naming the other surface, so the reader sees the connection.

## Step 1 gate + the confirmed data relationship

Re-confirmed both header markups match the card's own citations exactly before editing. Also
re-confirmed the relationship itself, not just the visual coincidence: Home's `EXPERT CURATED`
carousel reads `GET /api/mobile/home/approved-trades`; `/m/alerts` reads `GET /api/alerts/ideas` —
two different endpoints/renderers over the same underlying approved-trade rows, exactly what the
founder's two screenshots showed.

## The fix

- **Home** (`apTrShellHtml()`): `<span class="ap-tag">Trade Alerts</span>` right after the
  `EXPERT CURATED` eyebrow, before the rule/count. Bare `.ap-tag` (not `.b`/`.s`) — this is a
  neutral cross-reference, not a LONG/SHORT side badge, and `.ap-tag` is already used bare for the
  basket/engine name on the cards below it, so this matches an existing use of the same class, not
  a new visual pattern.
- **Alerts** (`.ia-top` / `.ia-h1`): `<div class="ia-tags"><span class="ia-tag mu">Expert
  Curated</span></div>`, placed inside `.ia-h1` after the existing `Curated · Equity & Futures`
  subtitle. Reuses `.ia-tags`/`.ia-tag.mu` verbatim — the same pill family the idea cards below
  already use for style/instrument tags — `.mu` (muted) specifically chosen over `.gd` (gold) so
  this new cross-reference tag is never visually confused with the page's own existing `EXPERT
  HANDPICKED` gold badge, a different, pre-existing label with a different meaning.

**Scope item 3, decided and stated as asked**: both tags are **static labels**, not tap-to-navigate.
This is the card's own explicit lowest-risk default, taken because the founder's ask was to show
the connection visually, not to add navigation. Named here as an easy, named follow-up if the
founder wants either tag tappable — not built unasked.

**Do not touch, respected**: no backend/endpoint change (labelling only). The filter sheet, the
Live/Closed + category tabs on `/m/alerts`, and both pages' card/idea-card content are untouched —
confirmed by diff and by the verify suite finding their existing ids/markup intact.

## Verify

`node --check` clean, both files. Real headless Chromium at a real 360px phone width, the actual
header markup **extracted verbatim from both committed files** (`apTrShellHtml()` + its real
`.ap-hdr`/`.ap-tag` CSS from `mobile/home.html`; the real static `.ia-top`/`.ia-h1`/`.ia-tags`/
`.ia-tag` markup + CSS from `mobile/alerts.html`) — **18/18 checks pass**:

- Both tags render with the exact class names asked for (`.ap-tag` bare, `.ia-tag.mu`) and the
  exact text (`Trade Alerts`, `Expert Curated`).
- Neither tag carries an `onclick` — confirmed static, not silently made tappable.
- Home's `EXPERT CURATED` eyebrow text and Alerts' existing subtitle are both confirmed unchanged
  and not crowded — read directly from the rendered DOM, not assumed from the markup alone.
- **Neither header overflows horizontally at a real 360px viewport** (`scrollWidth <= clientWidth`)
  — the card's own "does not shift or wrap awkwardly on a narrow phone width" verify requirement,
  checked with a real layout engine rather than eyeballed.
- `do_not_touch` confirmed: the filter-sheet/tab ids (`#ia-tabs`, `#ia-cats`) are present and
  unmodified.
- Zero page errors.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): the card's own
verify section marks confirming on-glass that the pairing "reads as the connection was intended to
communicate" as founder-only — a subjective/visual judgment this container cannot make.
