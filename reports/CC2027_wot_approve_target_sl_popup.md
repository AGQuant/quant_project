# cc#2027 — Rebuild Approve + target/SL capture on Wall of Trades

Founder-confirmed workflow (13-Sep-2026, voice): approving any trade, equity or futures, always
pops a target and stop-loss confirmation — pre-filled from the source engine when available, else
typed in manually — before the approval completes.

## Correction to the card's own "why" — read the live code, not the docstring

The card's framing states the Approve button was "never rebuilt" after cc#1537 deleted it. That is
the state of a **stale header comment** (`trade_wall_web.html` line ~1163, still describing
cc#1537's removal) that was never updated after **cc#1609 (WOT_APPROVAL_SURFACE_V1)** — dated
*after* cc#1537 — fully rebuilt the client-side APPROVE/DISMISS buttons, the click handler, the
approval-window gate and disable logic, on both `trade_wall_web.html` and `mobile/trade_wall.html`.
Confirmed by reading the actual code (`twCell()`/`twAct()`/`stateHtml()`/`act()`), not the comment
above it. **What was genuinely missing** — the only real gap — is the target/stop-loss confirmation
step: today's Approve fires `/api/alerts/approve_signal` immediately on click, with no popup and no
levels captured. This card closes exactly that gap, on both web and app (mobile/trade_wall.html is
"a Fable single-mode build," an app page, but this card names no explicit pusher, so it defaults to
CC per CC_DEFAULT_BUILD_RULE_V1 — and the founder's own words, "approving ANY trade... always,"
apply to both surfaces identically).

## Pre-fill — direct column reads only, never a computed or guessed number

Checked against real production data before writing a line of pre-fill code, per this project's own
data-honesty doctrine (never fabricate a number):

| Engine | Per-row fixed price level? | Source |
|---|---|---|
| **V8** | Yes — `v8_paper_positions.target`/`.stop_loss`, already computed by the engine. Verified: 22/22 open positions carry both. | direct column read |
| **TC Scanner** | Yes — `tc_scanner_holds.target`/`.sl`, already computed. | direct column read |
| **Index Intel** | **No** — `v10_trades` carries no target/stop_loss column at all; the engine's own exit is points off a *moving* SuperTrend close (dynamic, not a fixed entry-relative price) — confirmed by schema check, not assumed. | none — field left blank |
| **Investment Scanner** | No — this engine's own `engine-rules` text states "no target leg" explicitly. | none |
| **Screeners** | No — "no separate exit threshold in config" (own text). | none |
| **QB Basket** | N/A — carries no Approve button at all (cc#1843). | — |

A new **read-only** endpoint, `GET /api/tradewall/prefill-levels?engine=&symbol=&entry_ts=`
(`trade_wall_endpoints.py`), does exactly this lookup — matched on the same `(symbol, entry_ts)`
key `twRef()`/`ref()` already build client-side as `source_ref`, against the same naive-IST
`entry_ts` string format the wall's own `_EVENTS_SQL` already serializes. No formula, no direction
sign logic, no engine-internals guess — a value present in the popup means the engine's own stored
number; absent means the founder types it in, exactly as the card's own fallback instructs. This
was a deliberate, conservative scope decision: `/api/tradewall/engine-rules` (the card's other
suggested source) only carries human-readable prose (`"target X pct, stop Y pct"`), and converting
that to a price would mean inventing sign-convention arithmetic I could not verify against a live
case this session — the direct-column path was available and safe for two of three futures engines,
so that is what shipped; the one engine without a fixed price (Index Intel) genuinely has none.

## The popup

Both files: click Approve → a target/stop-loss popup opens (fetches the pre-fill above,
non-blocking — a failed fetch just leaves fields blank, never blocks the popup), both fields
optional and editable → Confirm calls the **existing** `approve_signal` endpoint, then — only if at
least one field was filled — the **existing** `POST /api/tradewall/approved/levels` (cc#1735) with
the `alert_id` the approve call just returned. Both calls in one confirm action, as specified. If
the founder leaves both blank, the second call is skipped entirely (nothing to save). If levels-save
fails after a successful approve, the approval still stands (it already happened) and the row's
message says so, pointing at the Approved tab's own existing inline editor as the fallback.

**Reused, not invented:** `trade_wall_web.html`'s popup is the file's own existing `.ov`/`.ov-box`
overlay markup (already used for the "Other engines" sheet) with two inputs added — same shell,
same close-on-veil-click, same Escape-key wiring extended to include it. `mobile/trade_wall.html`
had no prior overlay pattern, so its version is a bottom sheet in the same idiom as the rest of that
page (contract tokens, `.btn2`-style buttons) — the card's own instruction was to match existing
conventions, and each surface's existing convention differs, so each popup does too.

**Dismiss is completely untouched** — no popup, same direct call as before this card, on both
surfaces (`git diff` shows the only removed lines are the two click-dispatch functions being
replaced, nothing else in that path). The approval-window gate (cc#1760) is not re-implemented:
the button's disabled state and reason still come from the exact same server-stated field as
before; the popup's own open/confirm functions add a "belt and braces" re-check of that same field
(matching the phrase and pattern `twAct` already used) for the case the window closes while the
page sits open with the popup already showing.

## Do-not-touch, confirmed by diff

Server-side state-join/suppression/approval_window/not-approval-applicable logic in
`trade_wall_endpoints.py` — untouched (the only addition is the new, separate, read-only
prefill-levels endpoint). Alerts tab — no approve button added there, nothing touched. The
`trade_alert_levels` close/priority resolver (cc#1781) — untouched, this card only ever writes
`target_price`/`stop_loss` via the existing `/levels` endpoint's own upsert. QB Basket rows and
suppressed-in-position rows — zero lines changed in that branch of `twCell()`/`stateHtml()`,
provably unaffected.

## Verify — real Chromium, the real files, both surfaces, no reimplementation

`node --check` clean on every touched script block (2 in `trade_wall_web.html`, 2 in
`mobile/trade_wall.html`). Real Chromium ran the actual modified files against a fixture built
directly from the real response shapes (`_shape()` in `trade_wall_endpoints.py`,
`approve_signal`'s real return shape, `/levels`' real contract) — `/api/tradewall`,
`/api/tradewall/prefill-levels`, `/api/alerts/approve_signal`, `/api/tradewall/approved/levels`,
`/api/alerts/approval_window` all stubbed via `page.route`. **24/24 checks pass** across both
surfaces:

- V8 and TC Scanner rows pre-fill both fields from their real stored values; Index Intel leaves
  both blank with an honest "no stored level" message — never a guess.
- Confirming calls `approve_signal` then `approved/levels` with the exact `alert_id` the first call
  returned, and both entered values — verified on the actual request bodies, not assumed.
- Leaving both fields blank skips the levels call entirely (still exactly one call after two
  approvals, one with a level, one without).
- The row visually flips to APPROVED with no further click — checked under the wall's own "All"
  state filter, after confirming (and separately checking) that an approved row correctly *leaves*
  the default "Pending" filtered view — pre-existing filter behavior, not a regression, verified
  by diff that this card touched none of that logic.
- Cancel closes the popup with zero approve calls made; the row stays pending.
- Dismiss still fires directly with no popup, on both surfaces.
- Outside the approval window: the button still renders disabled with the server's own reason
  text (unchanged gate); calling the popup-open function directly (bypassing the disabled button
  entirely) proves its own defensive re-check still refuses, not just the HTML attribute.
- App surface: identical pre-fill, approve+levels sequencing, and APPROVED flip, confirmed
  separately against `mobile/trade_wall.html`'s own real code (`act()`/`find()`/`ref()`/`dir()`/`Q`).

One test-fixture gap found and fixed along the way, not a page defect: `mobile/trade_wall.html`'s
own pre-existing `act()` (unchanged by this card) already re-fetches the whole wall via `load()`
after any action, rather than a local re-render — a static mock doesn't reflect that back, so the
mock was made to track applied approvals and return them on the next fetch, matching what the real
server already does.

## Not done here (the card's own founder-only item)

A live approve on a real pending-approval row (or a scripted cursor check against the actual
endpoints) confirming `trade_alerts`/`trade_alert_levels` on the real database — this container has
no route to scorr.in and this card's write path is real-money-adjacent, so it was verified through
the real code and a faithful local harness rather than exercised against production. Fable's
DB-query verification (both seats' standing practice) is the intended next check.
