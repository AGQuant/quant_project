# cc#2030 — the bell becomes the Alert Center (web + app)

Founder voice, 13-Sep-2026, first spec on record for this component. Three parts: a triggered
feed (top, default view, notification-style, top 4-5 + View more), Set Alert (bottom-left, the
flow cc#2029 relocated off Wall of Trades), View Alerts (bottom-right, the pending/untriggered
list). **Critical isolation rule**: purely user-level — no Wall of Trades row, no engine signal,
no approval, ever appears here.

## Design first, per this project's own preview-then-promote convention

`previews/bell_alert_center.html` — a static mockup (no fetch, dummy data stamped as such) laying
out the three states this card's own layout describes: the default feed view, the pending list
("View Alerts expanded"), and the honest empty state (nothing triggered yet — never hidden, never
a stale badge). Produced and referenced here per the card's own verify instruction, before the
live build below.

## Verified against the real schema before touching a filter — the isolation rule's actual key

The founder's own wording — `kind='manual'` — **does not exist as a database value.** Read
`trade_alerts_endpoints.py`'s own constant: `KINDS = ("entry", "exit")`. An engine-approved row
(`approve_signal`) and a manual alert both default to `kind='entry'` — `kind` distinguishes
entry-vs-exit, not manual-vs-engine. The real, already-correct discriminator (unchanged since
cc#1696) is `source_engine IS NULL`, which is what `manualAlerts()`/`feedAlerts()` key on.

**cc#2030's own item 6 (CLEANUP) assumed leakage that, checked against real rows, does not exist
today.** The card's "why" states "old trade/engine-signal history leaking into the bell feed."
Queried `trade_alerts` directly: every row with `source_engine IS NULL` is `kind='entry',
status='pending'` — exactly one row, nothing else. The existing `!a.source_engine` filter (already
in the pre-cc#2030 code) already excludes every engine/QB row correctly; there was nothing to clean
up in the data. This rewrite changes the **layout only** — the filter was already right, and this
is now stated with evidence rather than assumed.

## The feed — a schema-honest rule, not a guess

`triggered_at` is the one reliable, real timestamp shared by every outcome a triggered alert can
reach (still awaiting a decision, later approved, or later dismissed) — confirmed by reading
`dismiss_alert()`: a dismissal writes only `status='dismissed'`, no timestamp of its own. The feed
is therefore `source_engine IS NULL AND triggered_at IS NOT NULL`, sorted `triggered_at DESC`. A
manual alert the founder approved directly from pending (the pre-trigger override, cc#1586) never
sets `triggered_at` and so is correctly excluded from "triggered" — it was never triggered, by
construction; inventing a fallback sort key for it would be a guess this project's own doctrine
forbids. Verified with a dedicated fixture row (`OVERRIDE_APPROVED`, `triggered_at: null`) proven
absent from the rendered feed.

Top 5 shown by default (the founder said "4-5"; 5 chosen, stated here), "View more" expands in
place to the full list, still newest-first — no pagination, no second fetch, everything already
loaded by the existing `/api/alerts/list` call.

## Set Alert — the relocated flow, verbatim, not rebuilt

`window.ScorrAlertCreate.open(load)` (cc#1831) is called directly — the exact module cc#2029 just
removed from Wall of Trades, unmodified, its `onCreated` callback wired to `load()` so the bell's
own data refreshes after a successful create. The bell popover closes first (one focused overlay
at a time, not two stacked). Making this reachable required two script-loading additions (the
module was previously only inline on the two pages that had "+ New alert" buttons, never
universally loaded):

- `main.py`'s `_MOBILE_HEAD` — every `/m/*` app page now also loads `/scorr_alert_create.js`
  (deferred, cache-stamped with the build id, same pattern as `scorr_bell.js` right above it).
- `pwa_endpoints.py`'s dynamic bell-mount injection — every web page carrying the shared nav now
  also gets the module injected, guarded against double-injection exactly like the bell's own tag.

## View Alerts — a toggle, not a navigation

Clicking "View Alerts" switches the SAME popover to `status='pending', source_engine IS NULL` rows
only (a triggered-but-undecided alert belongs in the feed above, never here — the two views are a
clean split on one boolean, not overlapping sets); the same button becomes "← Recent" and switches
back. Nothing navigates away — the bell stays the one surface, per the founder's own framing ("the
bell is the new home for alert").

## Do-not-touch, confirmed

The cc#1717 badge/seen mechanism (`badgeCount()`/`markSeen()`/`paint()`) — byte-identical to
before this card; verified live via the test (the POST to `/api/alerts/seen` still fires with
exactly the unseen ids on open). Wall of Trades' Approve+SL flow (cc#2027) and the Alerts
pure-display conversion — untouched, unrelated files. `trade_alerts` schema — no new columns; every
field this card reads already existed.

## Verify — real Chromium, the real files, no reimplementation

`node --check` clean on `scorr_bell.js`, and on the JS embedded in `main.py`'s `_MOBILE_HEAD`
addition and `pwa_endpoints.py`'s `PWA_JS` block (both `ast.parse`-clean as Python, and the
embedded JS extracted and `node --check`-ed separately). Real Chromium ran the actual rewritten
`scorr_bell.js` against the actual `scorr_alert_create.js` (not stubbed), with a fixture mixing 9
manual alerts (every outcome: still-triggered, approved-after-trigger, dismissed-after-trigger,
pending, and the approved-without-ever-triggering edge case) and 3 engine/WOT rows that must never
surface. **22/22 checks pass**:

- The isolation rule: zero engine rows appear anywhere in the rendered bell, checked against the
  actual page content, not assumed from the filter's presence in source.
- Feed defaults to the top 5, newest-triggered-first; the override-approved edge case is correctly
  excluded; View more reveals the true remaining count and expands to all 6, order preserved.
- All three post-trigger outcomes render distinctly and correctly.
- View Alerts shows exactly the 2 pending manual alerts and nothing else; the label and title
  switch correctly; toggling back returns to the (collapsed) feed.
- Set Alert closes the bell and opens the real `ScorrAlertCreate` overlay — its actual form fields
  present, Cancel closes it cleanly.
- The badge/seen mechanism fires exactly as before — unchanged, verified, not just asserted
  unchanged by omission.
- All three pre-existing close paths (veil, ×, Escape) still work.

## Not done here (explicitly deferred by the founder, or out of this card's scope)

The full parameter set for `Set Alert` — the founder explicitly deferred this to a later card; the
entry point opens the existing, unmodified create form as-is. Push notification delivery for a
newly-triggered alert — this card is the UI/data surface only, per its own scope. A live open on
scorr.in confirming the layout visually — no route to scorr.in from this container; the design
preview plus the 22/22 functional checks are the evidence available here.

## Observation carried over from cc#2029, still open

Two alert-creation forms now coexist: the Home grid's "Custom Alert" tile still opens
`mobile/alerts.html`'s own older form (cc#1507, untouched, out of scope for all four of this
session's alert cards), while the bell now opens `ScorrAlertCreate` (cc#1831). Whether these should
unify onto one form site-wide is a founder call, not made here.
