# cc#2058 — Digest Live News POLISH category filter (same class as cc#2056)

Founder ask: after cc#2056 (Intel's Stock Views slow-load) asked for a full app scan for the same
pattern. This is the second instance found, in `scorr_digest_mobile.html`'s Live News card (POLISH
tab, cc#1326 filter icon).

## Root cause, already diagnosed by the card, re-confirmed before editing

Both POLISH fetches (`loadRawNews()`'s initial load and `rnLoadOlder()`'s cursor continuation)
always requested `category=all`; the picked category was applied **client-side** in `rnApplyMode()`.
A second, separate problem cc#2056 didn't have: the filter menu's own counts
(`nwFilterMenuHtml()`) came from counting `RN.polish` (whatever happened to be loaded), so a
category that hadn't surfaced yet didn't even appear in the menu, and the numbers shown were
page-relative, not true totals.

## A blocking detail found before writing any fix code

`/api/news/polished`'s `category=` parameter is **not** the display string `RN_CAT`/`RN_CATS` hold
— `news_endpoints.py`'s own `_CANON_CAT` maps a lowercase snake_case *key* (`stock_views`,
`ai_editorial`, …) to the real stored category value. Confirmed `stock_views` **is** present in
that map (cc#725) — the card's own "`/api/news/polished` (already correct)" claim holds — but a
naive `category=` + `RN_CAT` concatenation would have silently sent `category=Stock+Views`, missed
`_CANON_CAT`, fallen through to the endpoint's own `else: cat = "all"`, and reproduced the exact
bug one level down. `scorr_news.html`'s own working `load()` was read directly to confirm the
translation is genuinely needed (its `curCat` state is *already* stored in snake_case) rather than
assumed. Fixed with a small `RN_CAT_API` display→key map, checked against `_CANON_CAT` by direct
comparison, not guessed.

## The fix

`loadRawNews()`'s inline POLISH-fetch body became a shared `rnFetchPolish(cat)`, called both for
the initial unfiltered load (`'ALL'`) and from `nwFilterPick(cat)` on a real category change — which
now resets `RN.polish`/`RN.polishOverflow`/`RN.polishCursor`/`RN.polishHasMore` and refetches scoped,
instead of re-filtering an ever-growing `category=all` set. `rnLoadOlder()`'s own cursor-continuation
fetch now carries `RN_CAT_API[RN_CAT]` too, so once a scoped session's overflow buffer is exhausted,
continued paging stays inside the same category rather than silently falling back to unfiltered. A
`polishReqId` token (same pattern as cc#2056's `reqId`) discards a stale, out-of-order response if
the reader picks another category before the first one resolves — incremented by `nwFilterPick`
itself so it invalidates *any* fetch in flight, `rnFetchPolish` or `rnLoadOlder`, not just a race
between two picks.

**Item 3 (the overflow buffer question), answered directly, not left open:** the cc#1365 buffer does
**not** become irrelevant under scoping — it stays exactly as useful, provided the category-scoped
fetch does the same 48h win/over split the ALL fetch does (it does, `rnFetchPolish` is one function
for both). The buffer just now holds rows within whatever scope is currently active, rather than
always the ALL set.

Menu counts (`nwFilterMenuHtml`) and the POLISH tab's own badge now read a new `RN.polishCatCounts`
— the server's full `category_counts`, stored from any successful response (it's the same
full-database breakdown regardless of which category was requested, confirmed by reading
`news_endpoints.py`'s own query: no category `WHERE` clause on that specific aggregate). A category
with a nonzero server count is listed even when none of its rows are loaded yet — the pre-fix bug.

**A consequence caught proactively, before it could ship as a second bug** (this session already
hit the identical mistake once, in cc#2056, and applied the lesson here before testing forced it):
the POLISH tab's own count badge previously read `RN.polish.length`, which under the OLD design was
always the true ALL-mode total. Under the new scoped-fetch design that would silently shrink to
whatever single category is active (e.g. "POLISH 2" while viewing Stock Views, even though hundreds
exist overall). Fixed with `_rnPolishTotal()` — sums `RN.polishCatCounts` — used both by the ALL
option in the filter menu and the tab badge itself, falling back to `RN.polish.length` only before
the first fetch resolves.

**Untouched, per `do_not_touch`:** `/api/news/polished` itself (confirmed already correct,
including for Stock Views), the RAW tab, archive search (`RN_SEARCH`), the cc#1365 grouped
ordering and the news bottom sheet.

## Verify

`node --check` clean (via extracted-inline-script check). Real headless Chromium, **verbatim line
ranges extracted from the real, committed file** (not retyped — each extraction verified to contain
this card's own fix markers before running, catching one of my own off-by-one slicing mistakes
before it could produce a false result), a mock backend replicating `_CANON_CAT` + `category_counts`
+ the real keyset-cursor contract — **26/26 checks pass**:

- The exact founder-reported case (a sparse category) fires **exactly one request**, correctly
  translated to the snake_case API key, with zero cross-category leakage.
- `RN.polishCatCounts` is populated from the real server response; the filter menu lists every
  category with a nonzero server count — including ones with zero rows currently loaded — and its
  ALL option, plus the POLISH tab badge, both show the true grand total while a filter is active.
- Switching back to ALL fires a genuinely fresh, unscoped request.
- The cc#1365 overflow-buffer-then-cursor sequence still works correctly post-refactor, and stays
  scoped to the active category once the buffer for that scope is exhausted.
- A rapid double category-switch (a slow first request artificially delayed past a fast second one)
  never lets the slower, now-stale response overwrite the faster, correct one.
- Zero page errors.

Not done here, and not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site) — the card's own verify section marks that step
FOUNDER-ONLY.
