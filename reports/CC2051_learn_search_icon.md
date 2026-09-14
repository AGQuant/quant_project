# cc#2051 — /m/learn: search icon beside ALL ARTICLES

Founder ask: next to the ALL ARTICLES / count header on the app Learn page, add a search icon so
the reader can search the 122 articles by keyword.

## Step 1 (read-only gate) — re-confirmed before editing

cc#2050 (Knowledge Galaxy banner) landed at `902d087` since this card was filed, shifting
`mobile/learn.html`'s line numbers. Re-read the current file before touching it: `draw()`'s row
build is unchanged in shape, still producing `<div class="sec"><h2>ALL ARTICLES</h2><span
class="hint">122 · newest first</span></div>` — the insertion point the card names is still
accurate, just at a new line number.

## What was reused, and what had to be genuinely new

The **matching rule and debounce timing are reused verbatim** from `scorr_news.html`'s
`applyKbFilters()`: lowercase substring match against `(title + ' ' + summary)`, 180ms debounce,
ANDed with the active category filter — no second algorithm invented.

The **UI shape could not be copied** — web's search box is a permanently-visible sidebar element;
this card's spec explicitly asks for an icon that reveals an inline input **in the same `.sec`
row**, which has no web precedent to port. That toggle-in-place interaction is new, and it created
a real risk web's own code never has to deal with: web's `applyKbFilters()` only ever touches
`#kb-list`, never its `<input>`, because the box always exists. Here, a naive "just call `draw()`
on every debounced keystroke" (mirroring how the category chip handler already calls a full
`draw()`) would **recreate the `<input>` element on every re-render and drop focus mid-type** —
each keystroke would require re-tapping the box. Caught before shipping, not after: the debounced
path now calls a new, narrower `renderLearnList()` that updates only the count text and the result
list, never the `.sec` row itself, so the input keeps focus and caret position through every
re-render. `draw()` (full rebuild, including the input) only runs on open/close/chip-change — all
points where losing focus is expected or irrelevant.

## The four pieces

1. **Icon in the row.** `secRowHtml(rows)` renders the hint/count span plus either a `.sec-ic`
   button (closed) or a `.sec-srch` input+clear pair (open), inside a new `.sec-r` wrapper that
   `.sec`'s existing `justify-content:space-between` naturally treats as the row's one right-hand
   child — no restructuring of `.sec` itself.
2. **Reveal, not navigate.** `openLearnSearch()` sets `SRCH_OPEN=true` and calls `draw()`; the
   input is created focused, caret at the end of any existing term. Nothing leaves the page, no
   sheet opens.
3. **Filter logic.** `filteredRows()` replaces the old inline `CAT`-only filter with `CAT` AND
   (when `Q` is set) the title+summary substring test — one function, used by both `draw()` and
   `renderLearnList()`, so the two can never drift apart.
4. **Count line + debounce.** The hint text reads "`N` match" once `Q` is set, "`N` · newest
   first" otherwise (`resultsHtml`/`secRowHtml` both key off `Q`). The clear (×) button's own
   visibility toggles **instantly** on every keystroke (matches web's un-debounced
   `kbClear.classList.toggle`); only the actual list re-filter is debounced 180ms.

`clearLearnSearch()` mirrors web's `clearKbSearch()` exactly: empties the term, refocuses the
input, and **does not collapse the box back to the icon** — same as web's search box, once
revealed, never hiding itself again. No separate close/collapse affordance was built; the spec
asks only for reveal + clear, and adding a second interaction the founder didn't ask for would be
scope this LOW-difficulty card doesn't call for. Flagging it here rather than building it silently:
a "tap the icon again (or tap outside) to collapse back to the icon" affordance is a reasonable
follow-up if wanted, not something assumed.

**Untouched, per scope:** the category chip filtering itself (`chipsHtml()`, the chip click
handler), per-article expand-in-place reading (`openArt()`, the `MD` cache), cc#2050's Knowledge
Galaxy banner and its own separate search-less overlay (confirmed still rendering correctly, and
per that card's own note, this card does not merge the two search boxes or share state between
them), and `/api/knowledge/articles` (no backend change — `D.articles` already in memory is all
this needed).

## Verify

`node --check` clean (via extracted-inline-script check). Real headless Chromium, the real
`mobile/learn.html`, a 5-article/3-category stubbed payload — **33/33 checks pass**:

- Icon renders inside the same `.sec` row as the title, no horizontal overflow at 390px.
- Tapping it swaps the icon for a focused input with no navigation; typing narrows the list only
  after the 180ms debounce (confirmed unchanged mid-window), matching by title **and** by
  summary-only text, case-insensitively; the count line updates to "`N` match" and the clear button
  appears.
- **Focus survives the debounced re-render** — the specific risk this design had to avoid — and the
  input's own value is untouched by it.
- Switching category chips while a search is active keeps the box open, refocuses it, and
  re-applies the same term scoped to the new category (a strict subset of the all-category match
  set, never category-blind).
- Clearing restores the full category-scoped list, reverts the count wording, hides the clear
  button, refocuses the input, and does **not** collapse the box.
- A zero-match search shows the correct search-specific empty state, not the category-empty
  wording and not a stale list.
- Regression: plain category filtering with no search, `openArt()`'s read-in-place, and cc#2050's
  Knowledge Galaxy banner (including its own correct article count) all still work unchanged.
- Zero real console/page errors.

Not done here, and not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site) — the card's own verify section marks that step
FOUNDER-ONLY.
