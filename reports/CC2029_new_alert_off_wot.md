# cc#2029 — Founder reversal: custom alert creation moves OFF Wall of Trades entirely

Founder, verbatim (13-Sep-2026): *"New alert button is not working, and it should not be there —
room for new alert is not here — it should be the bell — both on app and web the bell is the new
home for alert — create custom alert."* This reverses session_log 40507 part_3's earlier
instruction ("+New Alert moves to WOT") on **placement only** — 40507's other parts (Alerts pure
display, QB relocation) are unaffected and still stand.

## Item 1 — removed, one surface only had it

**`trade_wall_web.html`**: the `id="newAlertBtn"` button (`＋ New alert`, calling
`ScorrAlertCreate.open(...)`) is removed, along with its now-unused `<script
src="/scorr_alert_create.js">` tag (the button was the only call site on this page — nothing else
references the module here, confirmed by grep before removal).

**`mobile/trade_wall.html`**: had **no such button at all** — confirmed by reading the complete
152-line file. The card's own scope named both files defensively ("whatever ... currently exists
or was specified to exist on ... and mobile/trade_wall.html"); on the app surface nothing existed
to remove. Custom alert creation was never on the app's Wall of Trades page in the first place.

## Item 4 (the card's own numbering repeats "4" twice) — root cause, not assumed

Read `scorr_alert_create.js` (cc#1831) in full: it is a fully self-contained, page-agnostic overlay
— its own injected `<style>`, its own `position:fixed;inset:0` element appended directly to
`document.body`, zero dependency on host-page markup. That rules out "the module is broken." The
founder's own later correction (recorded verbatim in the card) confirms the real cause instead:
clicking the button DID open the popup, but it rendered off-screen mid-page rather than centred in
the viewport — a CSS containing-block symptom (something on this specific page changes what
`position:fixed` resolves against), not a functional failure. **Not chased further** — moot now
that the control is gone from this page, per the card's own explicit instruction not to spend time
fixing a positioning bug in a place the control no longer lives.

## Item 2/3 — the bell's current state, reported precisely, not assumed

The bell (`scorr_bell.js`) **already has a working creation entry point today** — but it is a
**different mechanism** than the one being removed from Wall of Trades, and stating that precisely
matters for cc#2030 (which explicitly says the new "Set Alert" button should open "the creation
flow relocated from Wall of Trades per cc#2029," i.e. `ScorrAlertCreate` specifically):

- **Today**: the bell's "+ New alert"/"+ NEW ALERT" links navigate to `alertsUrl() + '#new'`, which
  (as of this session's own cc#2025) correctly opens `mobile/alerts.html`'s own, older, separate
  creation form (`alOpen()`, cc#1507) — a real, working, but **different** UI/flow than
  `ScorrAlertCreate`.
- **What is NOT yet true**: the bell does not call `window.ScorrAlertCreate.open()` anywhere, on
  either surface. `scorr_alert_create.js` is not even script-loaded on the pages the bell mounts on
  today (it is only present on `trade_wall_web.html` — now removed — and `trade_alerts_web.html`).

**Deliberately not built here.** cc#2030 is the already-filed, design-first card whose own scope
item 3 is exactly this wiring ("Set Alert... opens the creation flow relocated from Wall of Trades
per cc#2029... reuse that flow's underlying insert logic verbatim"). Building a throwaway version
of that wiring now, only for cc#2030 to immediately replace it with its design-approved version,
would be the duplicated-effort exactly `CC_QUEUE_DRAIN_RULE_V1` exists to prevent. This card's own
verify item 2 ("bell opens a working custom alert creation flow that successfully inserts a manual
pending alert") **already passes today**, via the existing `#new`-hash mechanism — cc#2030 is what
upgrades it to `ScorrAlertCreate` specifically, per that card's own explicit scope.

## Observation for the founder (not decided here)

After this card, TWO alert-creation forms remain reachable in the app: the Home grid's "Custom
Alert" tile still opens `mobile/alerts.html`'s own `alOpen()` form (cc#1507, untouched — out of
scope for this card, not mentioned in any of the four cards' do-not-touch or scope), while the bell
(after cc#2030) will open `ScorrAlertCreate` (cc#1831, relocated from Wall of Trades). Flagging this
inconsistency rather than silently resolving it — whether the Home tile should also point at
`ScorrAlertCreate` for one form site-wide is a founder call, not decided or acted on here.

## Do-not-touch, confirmed

cc#2027's Approve + target/SL popup — untouched, re-verified working after this edit (regression
check below). The Alerts tab's pure-display conversion (session_log 40507 part_2) — unaffected,
separate from this reversal (and separately, cc#2028's audit landed just before this card found
that conversion is not yet complete on the web Alerts surface — a different page, a different
finding, not conflated here). The underlying `trade_alerts` manual-insert mechanism/schema —
untouched; only an entry point is being relocated, never rebuilt.

## Verify — real Chromium, the real file

`node --check` clean on the touched script block. Real Chromium loaded the actual modified
`trade_wall_web.html` — 5/5 checks: zero console errors (removing the now-dead script tag broke
nothing else on the page); `#newAlertBtn` is gone from the DOM; no rendered `<button>` anywhere on
the page reads "New alert" (checked against actual button text, not page-source substring, since
this card's own explanatory comments legitimately quote that phrase in prose); `scorr_alert_create.js`
is not even requested over the network any more (the tag is gone, not just hidden); and — the
regression check — cc#2027's Approve popup still opens correctly on this file after the edit.

## Not done here (the card's own founder-only item)

Confirming on the real deployed app that the bell's existing entry point still works exactly as it
did before this card (this container has no route to scorr.in) — the underlying file
(`mobile/alerts.html`) was not touched by this card at all, so nothing here could have regressed it;
stated rather than re-verified live.
