# cc#2056 — /m/intel: server-side category filtering for sparse categories

Founder report, screenshot: tapped the Stock Views chip on the app Intel page (5 of 3603 items in
the 60-day window) and it sat on "Nothing in this filter yet / Loading more… / showing 0 of 3603."
"Diagnose the issue."

## Root cause, already fully diagnosed by the card itself, re-confirmed before editing

`GET /api/mobile/intel` had no category parameter at all — every request pulled the next 40-item
keyset page of the **entire** mixed feed, and `mobile/intel.html`'s own `ensureFilterFilled()`
compensated by blindly calling `loadMore()` against that unfiltered feed, repeatedly, until enough
matching items happened to surface client-side. For a category at ~0.14% density that is close to
90 sequential round trips before the reader sees anything.

The file's own comment gave a reason for the design: *"Filtering server-side would make the chip
counts lie about a set the client only partly holds."* That reason doesn't hold — `category_counts`
is computed by its own separate, always-full-window `GROUP BY` query, completely independent of
the paginated feed. The chip counts were never derived from the loaded pages, so scoping the feed
query was never actually a risk to them. `/api/news/polished` (the desktop web page) already does
this correctly and was the pattern to mirror.

## The fix

**Backend** (`mobile_endpoints.py`, `mobile_intel()`): new optional `category` parameter, applied
identically to the header/count query (`head`) and the paginated feed query (`cols`) as a plain
parameterized `AND category = %s` — no hardcoded category allowlist, matching `category_counts`'
own registry-derived philosophy (an unmatched value naturally returns zero rows). `category_counts`
itself is untouched, per scope. One consequential fix beyond the letter of the card, needed to keep
the header honest: `head`'s count/newest/oldest are now scoped too, mirroring how
`/api/news/polished` derives its own category-scoped `total` from the same full-window counts
rather than a second query — so `header.count`/`total`/the "showing X of Y" cover line describe
the view actually being shown, not always the all-categories total.

**Frontend** (`mobile/intel.html`): the state model changed from "one big unfiltered list,
re-filtered client-side on every chip tap" to "one fetch per active category." `pick(cat)` now
calls `loadCategory()` — a fresh, category-scoped fetch that resets pagination state — instead of
re-rendering the already-loaded unfiltered set and blindly paging. `loadMore()` carries the active
category on every continued page, so pagination within a category that's large enough to need it
(tested with 50 items) stays scoped throughout. `ensureFilterFilled()` is genuinely dead once the
fetch itself is scoped — removed entirely, per the card's own invitation to do so rather than leave
an inert safety net. The client-side `.filter()` calls in `render()`/`visibleCount()`/
`appendCards()` are equally dead under the new design (the server already scopes every response) —
removed too, for the same reason, guarded against a real race a fresh-fetch-per-tap design
introduces that the old one never had to worry about: a `reqId` token discards a stale, out-of-order
response if the reader switches categories again before the first fetch resolves, so a slow first
request can never overwrite a faster second one.

**A bug the backend change would have introduced on its own, caught before shipping:**
`chipsHtml()`'s "All" chip read `header.count` for its own displayed total — correct under the old
semantics (always unfiltered), but wrong the moment `header.count` becomes category-scoped: the All
chip would have shown whatever category was currently active's count instead of the true grand
total. Fixed by deriving the All chip's count from `category_counts` (summed, untouched,
full-window) instead — confirmed the All chip still reads the true total while a filter is active.

**Untouched, per `do_not_touch`:** `category_counts`' own query, the 60-day window
(`INTEL_WINDOW_DAYS`), the keyset cursor mechanism itself (format and comparison logic unchanged,
just additionally scoped), the AI Editorial/feed split, `scorr_news.html`/`/api/news/polished`.

## Verify

`ast.parse` clean on `mobile_endpoints.py`; `node --check` clean on `mobile/intel.html`'s inline
JS. Real headless Chromium, the real `mobile/intel.html` against a mock backend that replicates
the real keyset-cursor + category contract (not a simplified stand-in: 50/10/2/0-item categories
across a 60-day spread, exercising a single page, a genuinely sparse case, multi-page pagination,
and a genuinely empty one) — **22/22 checks pass**:

- The exact founder-reported case (a sparse category) now fires **exactly one request**, carrying
  `category=`, and both its items render with zero cross-category leakage; the cover line reads
  the true category-scoped total, not the all-categories count.
- The **All chip keeps showing the true grand total** while a filter is active (the bug the backend
  change alone would have introduced, caught and fixed).
- A category large enough to need a second page (50 items) pages correctly: the first request
  returns 40, `loadMore()` carries the same `category` and a cursor, and all 50 end up rendered
  with no leakage.
- Switching back to All fires a genuinely fresh, unscoped request and shows a real mix of
  categories again — not leftover filtered data.
- A genuinely empty category shows an honest "nothing here" message that does **not** claim to
  still be loading or paging.
- A rapid double category-switch (a slow first request artificially delayed past a fast second
  one) renders only the latest selection — the `reqId` guard discards the stale response.
- Zero real console/page errors.

Not done here, and not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site) — the card's own verify section marks that step
FOUNDER-ONLY.
