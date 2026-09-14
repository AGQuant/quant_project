# cc#2049 — Home's EXPERT CURATED filter sheet: Closed navigates, it doesn't filter

Scope: the Approved Trades (EXPERT CURATED) filter sheet on `mobile/home.html` gets a 4th option,
Closed — but `APTR.rows` is this component's own **OPEN** approved book only (its own code
comment says so), so there is no closed-trade data here to filter against. Closed has to send the
founder to the page that actually has it (`/m/alerts`) instead of pretending to filter in place.

## The four changes

**`mobile/home.html`**
1. `APTR_FILTERS` gets a 4th entry: `{ key: 'closed', label: 'Closed', nav: '/m/alerts#closed' }`.
   `nav` (not `test`) is how the next two functions tell this entry apart from the three real
   filters, without guessing from the key name.
2. `apTrFilterOpen()`'s per-option render loop: guarded against `f.test` being undefined for the
   nav entry — `on` is forced false and the count (`n`) is `null` for a nav entry, rendered as an
   em dash (`—`) rather than `0` or any fabricated number.
3. `apTrFilterPick(key)`: looks the key up in `APTR_FILTERS` (a fresh lookup, since the existing
   `apTrFilterDef()` helper looks up the *currently active* filter, not an arbitrary key). If the
   match has `.nav`, it closes the sheet and does `window.location.href = f.nav` — `APTR.filter`
   and `apTrRender()` are never touched. Otherwise, behaviour is exactly what it was before this
   card (toggle the filter, close the sheet, re-render).

**`mobile/alerts.html`**
4. New `checkClosedHash()`, added alongside the existing `checkNewHash()` (same same-document-
   hash-change reasoning the founder's bell popover already established: a `#closed` link doesn't
   reload the page, so a one-shot parse-time check would miss it). On `#closed`: clears the hash,
   sets `IA.tab = 'closed'`, `IA.cat = 'all'`, and calls the real `renderFilters()`/`renderList()`
   — the exact same three lines the page's own tab-click handler already uses for a live tab
   switch, not new logic. No busy-guard was added (unlike `checkNewHash()`'s): `checkNewHash()`
   needs one because its continuation is a `setTimeout(...,0)`, which can re-enter before it
   fires; `checkClosedHash()` has no async continuation at all, so it can't re-enter itself.

## Why `apTrVisible()` needed no change

`apTrVisible()` (the function that actually filters the carousel) calls `apTrFilterDef()`, which
only ever returns an entry found via `APTR.filter === <key>`. Since `apTrFilterPick()` returns
early for a nav entry *before* ever assigning `APTR.filter`, `APTR.filter` can never become
`'closed'` — so `apTrVisible()` can never be handed the nav entry's missing `.test` and never
needed a guard of its own. Left untouched.

**Untouched, per scope:** `apTrRender()`, `apTrVisible()`, `apTrFilterDef()`, the three real
filters' own predicates, `checkNewHash()` and the bell's `#new` flow, everything else on both
pages.

## Verify

`node --check` clean on both files (via an extracted-inline-script check — `node --check` can't
take `.html` directly). Real headless Chromium, two harnesses built from the **verbatim committed
source** (line-sliced out of the real files, not retyped) — **26/26 checks pass**:

- The sheet renders exactly 4 options; Futures Long/Short/Equity counts are correct (2/1/3 for a
  6-row fixture); **Closed shows `—`, never `0` or a guess**; Closed never carries the active
  (`on`) tick class.
- A real filter (`fl`) still sets `APTR.filter`, still calls `apTrRender()`, still toggles back to
  All on a second tap — all exactly as before this card.
- Picking `closed` navigates to `/m/alerts#closed` (captured via route interception rather than
  letting the test actually sail away) and does **not** touch `APTR.filter` or call
  `apTrRender()`.
- Landing on `mobile/alerts.html` with `#closed` already in the URL (Home's new link) *and* via a
  same-document `hashchange` (the bell-popover-style case) both set `IA.tab='closed'`,
  `IA.cat='all'`, clear the hash, and call `renderFilters()`/`renderList()`.
- Regression: `#new` still clears its own hash and still calls `alOpen()` exactly once; `#closed`
  and `#new` do not cross-trigger each other; an unrelated hash triggers neither handler.
- Zero real console/page errors (one `favicon.ico` 404 from Chromium's automatic probe against the
  bare test server was traced by URL and confirmed unrelated to the page's own code, then
  excluded from the count rather than silently ignored as a blanket pass).

Not done here, and not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site, the same structural limitation on every UI card this
session).
