# cc#2065 — rename Home grid tile Intel → News Intel

Founder ask: the plain "Intel" tile in the Market grid (beside Sector Intel and Index Intel)
should read "News Intel" for consistency.

## Step 1 gate — one correction to the card's own framing, confirmed by evidence

`G_GROUPS`'s Market-group tile (`mobile/home.html`) matched the card exactly (line drifted to
~1999, not ~1982 — line numbers move as the file grows, re-confirmed rather than trusted). The
card flagged a second `gtile('/m/intel', '▤', 'Intel')` reference as "not confirmed dead" and
asked me to check. Traced it precisely: it sits inside `renderHome()`'s own `tools` div — 15
tiles, built with `document.createElement`/`.innerHTML`, but **that div's own `appendChild` call
is explicitly commented out** (`// document.getElementById('body').appendChild(tools);`). It is
built every render and never attached to the document — genuinely, provably dead, for a more
specific and different reason than the card's own guess ("superseded by G_GROUPS"). Left
untouched per the card's own instruction and this file's own established disclosed-dead-code
convention — the exact finding, not a guess, is stated here and in the push log.

## The fix

- `mobile/home.html`: the Market group's first tile label — `'Intel'` → `'News Intel'`. Href,
  icon, and grid position all unchanged.
- `mobile/intel.html`: all three same-page occurrences of the bare label updated to match —
  `<title>`, the shared appshell header `<span class="t">`, and the hidden `<h1>` fallback.

**Do not touch, respected**: Sector Intel, Index Intel (a real, separate tile at `/m/v10`, third
in the Market group — confirmed present and untouched, not to be confused with the dead tools-list
reference above), Investment Scanner, and every other grid tile/group are all unchanged — confirmed
by diff. `scorr_news.html` (web) was checked and confirmed already distinctly named, out of scope.
The `/m/intel` route itself is unchanged — only the label moved.

## Verify

`node --check` clean, both files. The real `G_GROUPS` array and `mobile/intel.html`'s three label
spots, extracted verbatim from the committed files — **13/13 checks pass**:

- The Market group's first tile reads `News Intel`, same href, same position — confirmed it is
  still the first tile in the group, not accidentally reordered.
- Exactly one occurrence of `News Intel` in `G_GROUPS` — the rename didn't leak anywhere else.
- Sector Intel and the real Index Intel tile (`/m/v10`) are both present and byte-unchanged.
- The dead `gtile('/m/intel', ..., 'Intel')` reference is confirmed still exactly as it was, and
  its own `tools` div is confirmed still never appended — genuinely dead, left alone as scoped.
- All three `mobile/intel.html` spots read `News Intel`; no stale bare `Intel` remains in any of
  them.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that tapping the renamed Home tile lands on a page whose own header now reads News Intel.
