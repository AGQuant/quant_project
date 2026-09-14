# cc#2059 — /m/trades engine chip: server-side scoping (milder cc#2056 class)

Founder ask: full-app scan for the cc#2056 slow-loading pattern found a third, milder instance in
`mobile/trade_wall.html`.

## Root cause, re-confirmed before editing

`status` and `instrument` were already server-side (`_fetch()` applies both in SQL). The `engine`
chip alone was client-side: `draw()` filtered whatever 40-row pages had already loaded
(`Q.events.filter(e => e.engine === Q.eng)`), while the chip's own count came from `by_engine`, a
true whole-scope total the server computes separately. Picking a rare engine could show far fewer
rows than its own chip count, needing repeated "Load more" taps to fill in — milder than cc#2056
because paging here is manual, never hangs, but the count-vs-list mismatch is the same honesty gap.

## The fix

- **`trade_wall_endpoints.py`**: `tradewall()` gains an `engine: str = ""` parameter; `_fetch()`
  gains the matching `WHERE engine = %s` clause, applied the same way `instrument`/`status` already
  are. `engine` has no fixed enum to validate against (unlike `instrument`/`status`) — it's read
  live off the wall itself via `by_engine`, so a hardcoded allowlist here would be exactly the
  staleness risk this app's own cc#1587 pattern already avoids elsewhere; an unmatched value
  legitimately returns zero rows, not a rejected request. `by_engine`'s own totals query (a
  separate block, untouched) stays computed over the unfiltered scope, per the card's own
  instruction — confirmed by reading it, not just by not editing it. `/api/mobile/tradewall` (a
  thin alias forwarding to the same function) now forwards `engine` too, for consistency — it isn't
  used by this page, but leaving it silently behind the canonical route would be the more
  surprising inconsistency for a "same function, one computation" alias.
- **`mobile/trade_wall.html`**: the engine chip's click handler now calls `load()` (a fresh,
  server-scoped fetch), exactly matching how the status/instrument chips already behave, instead of
  re-drawing whatever was already loaded. `draw()`'s client-side `.filter()` line is gone — the
  server now returns the right rows directly. `load()`'s fetch URL carries `Q.eng`.
- **Proactive, not asked for, kept minimal**: a `Q.reqId` staleness guard, the same pattern already
  shipped in cc#2056 and cc#2058 for this exact class of card (a fresh-fetch-on-chip-tap flow).
  This is the third instance of that flow in this app; adding the same three-line guard here is
  consistent with the lesson already applied twice, not new scope — a rapid double chip-switch can
  no longer let a slower, now-stale response overwrite a faster, correct one.

**Do not touch, respected**: the approve/dismiss flow, approval-window polling, and the closed-book
summary block are all untouched — confirmed by diff.

## A related, real defect found while testing — filed separately, not fixed here

Building the real-Chromium test for this card empirically confirmed (not just theorised) a
pre-existing, unrelated issue: `draw()` rebuilds the ENTIRE header — the status seg row, the
instrument chips, and the engine chips — from every `/api/tradewall` response, including a
"Load more" (cursor) page. The backend always returns the `by_engine`/`counts`/`status_counts`
keys, but they're only ever populated on a fresh (no-cursor) request; on a cursor page they're
empty dicts. A "Load more" tap therefore silently blanks the whole chip/count header until the next
fresh load. This affects all three chip rows equally (not something my engine fix introduces or
worsens) and is out of this card's own stated scope. Filed as **cc#2071** (P2) with the exact
mechanism and a recommended fix (cache the last fresh response's header fields in `Q`, reuse them
on an append call) rather than silently patched here or silently left for someone to rediscover.

## Verify

`node --check` clean, both files affected (`mobile/trade_wall.html`); `ast.parse` clean
(`trade_wall_endpoints.py`). Real headless Chromium, the actual extracted `draw()`/`load()` code
**run against the real committed file**, with a mock backend replicating the real keyset+engine
contract precisely (including the exact response shape's always-present-but-sometimes-empty
`by_engine`/`counts`/`status_counts` keys) — **17/17 checks pass**:

- Engine chips render with the real unfiltered `by_engine` counts on load.
- Picking the founder-reported case (a rare engine) fires **exactly one request**, correctly
  scoped, with the list count now matching the chip count exactly — and every rendered row is
  confirmed to actually belong to that engine (no cross-category leakage).
- Switching status still clears the engine filter — the pre-existing behaviour, confirmed
  unaffected by this change.
- A rapid double engine-switch (a slow first request artificially delayed past a fast second one)
  never lets the slower, now-stale response overwrite the faster, correct one.
- The Load-more chip-vanishing defect above was reproduced directly, empirically, before being
  filed — not assumed from reading the code alone.
- Zero page errors throughout.

Not done here, not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site) — the card's own verify section marks that step
FOUNDER-ONLY.
