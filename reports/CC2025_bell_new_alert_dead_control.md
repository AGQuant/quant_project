# cc#2025 — Bell "+ NEW ALERT" is a dead control on /m/alerts: same-page hash nav, no hashchange listener

Founder screenshot, 12-Sep-2026 22:41 IST, on `/m/alerts` with the bell popover open: header reads
`Custom alerts / NONE SET`, body `No custom alerts set.` with the cyan `+ NEW ALERT` button, footer
carries `+ New alert` and `Open Alerts`. Founder: *"this new alert function in the notification
bell is not working, is not clickable."* The `NONE SET` state itself is correct (`trade_alerts`
holds zero source_engine-less rows today, cc#1696 scope 5's manual-only filter) — the defect is
only in the control.

## Root cause — two faults, verified by reading, not inferred from the screenshot

**Fault 1 (`mobile/alerts.html`):** the `#new` open check was a bare top-level statement —
`if (location.hash === '#new'){ location.hash = ''; setTimeout(alOpen, 0); }` — that runs exactly
ONCE, at initial script parse time. `scorr_bell.js`'s `render()` emits every "New alert" anchor as
`href="' + alertsUrl() + '#new"`, and `alertsUrl()` returns `/m/alerts` on a narrow viewport — which
is precisely where the bell already mounts (`_MOBILE_HEAD`). Clicking that link **while already on
`/m/alerts`** is a same-document hash change: the browser sets `location.hash`, does not reload, and
the one-shot check — having already run once, at page load — never runs again. Nothing opens.

**Fault 2 (`scorr_bell.js`), compounding it:** the overlay's click handler was
`if(e.target === ov || e.target.closest('[data-bell-close]')) close();` — a click on a link INSIDE
the popover matches neither branch, so `close()` never fires and the (dead-looking) popover stays
on screen. Combined with fault 1 the control reads as completely unresponsive rather than as a
navigation that silently went nowhere.

## Fix A — `mobile/alerts.html`: a named, re-entrant-guarded function, run on load AND on hashchange

```js
var _newHashBusy = false;
function checkNewHash(){
  if (location.hash !== '#new' || _newHashBusy) return;
  _newHashBusy = true;
  location.hash = '';
  setTimeout(function(){ alOpen(); _newHashBusy = false; }, 0);
}
checkNewHash();
window.addEventListener('hashchange', checkNewHash);
```

Same behaviour inside the guard as before (clear the hash, open `alOpen()` on a zero timeout) —
only the trigger changed: once at load (covers a fresh/cross-page arrival, e.g. Home's Custom Alert
tile) and again on every `hashchange` (covers the same-page case, the actual defect). `_newHashBusy`
stops the hash-clear's own resulting `hashchange` from re-entering and opening a second sheet.

## Fix B — `scorr_bell.js`: the overlay closes on an in-popover anchor click too

```js
ov.addEventListener('click', function(e){ if(e.target === ov || e.target.closest('[data-bell-close]') || e.target.closest('#scorr-bell-box a')) close(); });
```

One added `closest()` branch, no `preventDefault()` — the anchor's own navigation or hash change
still happens; `close()` runs alongside it, not instead. On a cross-page navigation the unload makes
this moot; on the same-page hash case it is what stops a dead-looking overlay sitting over the New
Alert sheet that Fix A just opened underneath it.

## Item 5 — every other `#new` entry point checked, not assumed

```
mobile/home.html   (gtile() + gtileR1(), two tile lists)  -> /m/alerts#new   CROSS-PAGE (real <a href>, different path)
mobile/myalerts.html (card())                             -> /m/alerts#new   CROSS-PAGE (real <a href>, different path)
scorr_bell.js (empty-state + footer link)                 -> alertsUrl()+'#new'  SAME-PAGE when the bell is open ON /m/alerts; CROSS-PAGE from any other app page
design_refs/scorr_home_grid_R1.html                       -> a design reference file, not live code — not applicable
```

Both `gtile()` (line 1969) and `gtileR1()` (line 2050) emit a real `<a href="...">` anchor — every
Home-grid / My Alerts entry point is a genuine cross-page navigation to a different path, which the
browser fully reloads, so the (now-named) `checkNewHash()` correctly runs again on load. These
already worked before this card and are unchanged by it, confirmed by reading, not assumed.

The bell mounts on every app page (`_MOBILE_HEAD`) and, on desktop width, the web nav (cc#1696) —
so its own `#new` link is same-page only when the bell happens to be opened while already on
`/m/alerts` itself (mobile width); from any other app page it is a real cross-page navigation and
already worked. **One pre-existing gap noted, out of scope for this card:** on desktop width,
`alertsUrl()` points at `/alerts` (`trade_alerts_web.html`), which has **no** `#new` handling of any
kind (grepped: no `#new`, no `hashchange`, no `location.hash` in that file) — the bell's "New alert"
links have never opened anything there, before or after this push. Not a regression introduced
here, not touched here (do-not-touch scopes this card to `mobile/alerts.html` + `scorr_bell.js`);
flagged for a founder call on whether the web Alerts page should grow the same flow.

## Do-not-touch, confirmed

`alOpen`/`alSearch`/`alSym`/`alPick`/`alSubmit` and the `al-*` CSS — zero changes, only the entry
point was broken. `/api/alerts/create`, `/list`, `/seen`, `/api/gvm/search` — no endpoint changes.
`alertsUrl()`'s narrow/wide branch and the cc#1717 seen/badge mechanism (`markSeen`/`badgeCount`/
`paint`) — confirmed untouched by `git diff` (the only change in `scorr_bell.js` is the one
`ov.addEventListener` line). The bell's manual-only filter and the `NONE SET` state — correct,
not a bug, not touched. The cc#1634 `#ia-<id>` deep-link in `mobile/alerts.html` — untouched code,
re-verified it still works alongside the new `hashchange` listener (a different hash pattern, no
interference).

## Verify — real Chromium, the real files, no reimplementation

`node --check` clean on `scorr_bell.js` and on both inline `<script>` blocks of `mobile/alerts.html`.
Two real-Chromium Playwright harnesses (one per file) against the actual shipped functions
(`checkNewHash`/`alOpen` and `scorr_bell.js`'s `open`/`render`/the overlay handler), fixture-driven
via `page.route` — 17/17 checks pass:

- Cross-page arrival at `#new` still opens the sheet on load (no regression); hash clears after.
- **The actual defect, reproduced and fixed:** already on the page, a same-document
  `location.hash = '#new'` now opens the sheet (it did not, before this push).
- The hash-clear's own resulting `hashchange` does not re-enter and open a second sheet (exactly
  one `#alWrap` mounted, checked via `eval_on_selector_all`, not assumed from a lack of errors).
- A second same-page `#new` hashchange still opens the sheet — the busy guard resets, it does not
  permanently latch after the first open.
- The cc#1634 `#ia-<id>` deep-link still lands correctly and does not trip the new listener.
- The bell popover opens on click; a click on the in-popover "+ New alert" anchor closes the
  popover AND the anchor's own hash navigation still occurs (not `preventDefault`-ed) — Fix B.
- The pre-existing close paths (veil click, × button) still close it — unchanged, re-verified.
- A non-anchor click inside the box (the "Custom alerts" header title, deliberately chosen since
  the box's own geometric center lands on the single open-alert row's link and would otherwise
  correctly close it too, not a bug) does **not** close the popover — Fix B did not over-broaden
  the close condition to "anywhere in the box".

One test-harness-only issue found and fixed along the way, not a page defect: Playwright's default
`wait_for_selector` visibility check failed against `#alWrap` itself, because that wrapper `<div>`
has only `position:fixed` children and so has zero own box size (real, on-screen content notwith-
standing) — switched the wait target to `.al-sheet` (the actual laid-out box) and kept `#alWrap`
for plain existence checks via `query_selector`, which does not require visibility.

## Not done here (the card's own FOUNDER-ONLY item)

Live taps on `/m/alerts`, `/m/home`, and a third app page (bell → + NEW ALERT / footer link /
Home grid tile) against the real deployed app, plus the badge-clears-on-open confirmation — this
container has no route to scorr.in, stated in the card itself, not worked around. The badge/seen
mechanism was instead confirmed untouched by inspecting the diff directly (see Do-not-touch above).
