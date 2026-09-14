# cc#2071 — /m/trades chip/count header vanishes after Load more

CC's own follow-up, found and empirically confirmed while testing cc#2059, not a founder-filed
card.

## Root cause, re-confirmed before editing

`draw(d, append)` rebuilds `#body`'s entire innerHTML from every `/api/tradewall` response,
including a "Load more" (cursor) page — the status seg row, the instrument chips, the engine
chips, and the top `asof` line, not just the row list. The backend always returns the
`by_engine`/`counts`/`status_counts`/`scope`/`instruments`/`closed_summary`/`approval_counts` keys,
but only ever *populates* them on a fresh (no-cursor) request — on a cursor page they're empty
(`trade_wall_endpoints.py`'s `tradewall()` initialises `counts`/`status_counts`/`totals` to `{}`
before its `if not cursor:` block and only fills them inside it). Reading straight off that
response therefore rebuilt the whole header from empty data on every Load-more tap, and the entire
chip row visibly disappeared — reproduced directly in a real-Chromium test, not assumed from
reading the code.

**Checked, per this card's own scope item 2, not assumed**: `trade_wall_web.html` (the desktop
counterpart) does **not** have this defect, for an architectural reason worth stating precisely —
its `loadMore()` function is entirely separate from `boot()` (the fresh-load path) and never
touches `STATE.counts`/`STATE.byEngine`/etc. at all; it only appends `STATE.events` and calls
`render()`, so the header state from the last real fetch is simply never overwritten. One adjacent
detail worth a note for whoever next touches that file: `boot()`'s own `keepCounts` guard (used on
a status/instrument switch, not pagination) protects `counts`/`statusCounts`/`total` but not
`byEngine`, which sits just outside it — harmless today because `boot()` only ever runs on a fresh
request (so `by_engine` is always freshly correct there), but an asymmetry worth knowing about if
that guard's scope ever needs to widen.

## The fix

`Q.hdr` caches the header-only fields (`status_counts`, `by_engine`, `approval_counts`, `scope`,
`counts`, `instruments`, `closed_summary`) from the last **fresh** response. `draw()` now reads
`sc`/`eng`/`ac`/`ic`/etc. from `Q.hdr`, not from the just-arrived response `d` — populated on every
`!append` call, left untouched (and therefore still holding the last real values) on an `append`
call. `Q.aw` (approval window) already had its own `||Q.aw` fallback and needed no change — checked
directly rather than assumed safe.

**Do not touch, respected**: the row list itself, sorting, the approve/dismiss flow, and
`Q.reqId`/the engine-scoping fix from cc#2059 are all untouched — confirmed by diff and by
re-running cc#2059's own full test suite against this change with zero regressions.

## Verify

`node --check` clean. Real headless Chromium, the actual extracted `draw()`/`load()` run against
the real committed file, with a mock backend replicating the real response contract precisely
(including its always-present-but-empty-on-cursor keys) — **13/13 new checks pass**, plus
**17/17 of cc#2059's own suite re-run clean (zero regression)**:

- The `asof` line, the status seg row, the instrument chip row, and the engine chip row are all
  confirmed **byte-identical** before and after a real Load-more click — not just "still present",
  the exact HTML is unchanged.
- The row list itself is confirmed to have actually grown (60 of 60 rows loaded across both pages)
  — the one thing that should change.
- The closed tab's Record section renders correctly on its own fresh load; confirmed (not assumed)
  that this fixture's closed set fits in one page, so a second, Load-more-specific check for the
  Record section surviving pagination is a natural next case once a fixture needs one.
- Zero page errors throughout.

Not done here, not needed for this card: the founder's own live tap-through on scorr.in.
