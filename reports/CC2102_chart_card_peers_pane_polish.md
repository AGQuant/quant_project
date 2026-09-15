# cc#2102 — V8 Chart Card Peers pane: S.Buy label, merged GVM+Band column, sort affordance

## Ground truth first (item 3)
Confirmed by reading the code directly, not assumed: `_bandChip()` in `scorr_chart_card.js`
hardcoded the four full band strings with **no** short-label variant, exactly matching the spec's
own evidence. `main.py` injects this file site-wide (every mobile page); `mobile/v8.html` itself
has zero references to peers/Strong Buy/PSU Banks. Both facts together confirm the founder's
screenshot reflects **this shared component's current live code**, not a stale or mobile-only
fork — stated here, per the card's own instruction, rather than assumed.

## What shipped

### Item 1: Strong Buy → S.Buy (pill only)
`_bandChip(b)` now renders a short label (`S.Buy`) only when `b === "Strong Buy"`; every other
band string is unchanged. The colour logic still keys off the full `b` value — untouched. The
header's `band_rule` legend text (server-provided, spelling out the full thresholds) is a
completely separate string and was never touched.

### Item 2: GVM score and Band tag merge into one column
The two were separate `<td>`s (and a separate "Band" `<th>` with no sort affordance of its own,
since Band was never a sort key — confirmed in `_PEER_KEY`). Merged into one cell
(`<span>score</span> <bandpill>`), and the standalone Band header removed — GVM's existing
sortable header now covers the merged column. The table drops from 7 columns to 6; the drawer
placeholder row's `colspan` updated `7→6` to match (the drawer's own logic, `_toggleDrawer`, is
untouched — this is only the static column count it sits inside).

### Item 4: sort affordance made visible before the first tap
The sort mechanism itself (`_peerSortBy`/`_peerOrder`, cc#987) was already fully wired — buttons,
click handlers, and a solid ▼/▲ arrow once a column becomes active. What was missing: an
**unsorted** header showed plain text with no visual cue it was tappable at all, which matches the
founder's screenshot (no visible sortable-header row). Added a faint always-visible `⇅` glyph
(opacity .4) to every sortable header, which becomes the existing solid arrow once that column is
active — same buttons, same `_peerSortBy`, no second sort implementation, exactly as the card's
own instruction required.

## Site-wide by construction (do_not_touch's own note)
`scorr_chart_card.js`'s Peers pane is one function (`_renderPeers`) with no page-identifying
parameter — grepped every caller of `ScorrChartCard.open`/`scorrPeerPane` in the repo (13 files,
including `mobile/gvm.html`, `v8_dashboard.html`, `v10_dashboard.html`) and confirmed `_renderPeers`
is the **only** definition anywhere — there is no second copy for GVM or SmartGain to diverge from.
This fix therefore applies identically regardless of which page opened the card.

## Verification
- `node --check` clean on the full file.
- Fallback ratchet (`.js` files are in scope): 1 → 1, no new hits. Raw-primitive ratchet: this
  file isn't in the tracked baseline (all styling is inline `style="..."` in JS strings, not a
  `<style>` block), so it's unmeasured rather than gated — checked directly rather than assumed.
- **Real-browser test (Playwright), the founder's own screenshot data** — not constructed: the 8
  real PSU Banks peer rows (MAHABANK/BANKINDIA/PNB/UNIONBANK/BANKBARODA/SBIN/CANBK/INDIANB) with
  their real GVM scores and bands, copied verbatim from the task spec's own evidence section, run
  through the actual committed `_renderPeers`/`_bandChip`/`_peerOrder`/`_peerSortBy` (extracted via
  brace-matching, not retyped). **13/13 assertions pass**, including:
  - MAHABANK's real Strong Buy pill renders "S.Buy"; PNB/CANBK's real Buy/Watch pills are
    byte-unchanged.
  - The table is 6 columns with no standalone "Band" header; MAHABANK's merged cell contains both
    the real "8.07" and the "S.Buy" pill together.
  - Every sortable header shows a visible glyph before any tap (previously blank).
  - Tapping the real GVM header actually reorders the real 8 rows by their real GVM values,
    correctly descending on first tap and exactly reversed on the second — not just that a click
    handler exists, but that the real sort produces the real, correct order.
  - CANBK's pre-existing "THIS" tag (unrelated to this card) still renders.

## What did NOT change
The trade-card drawer (`_toggleDrawer`/`_drawerHtml`) and `/api/chart/tradecard` — only its
placeholder `colspan` number was updated to match the new column count. The Chart tab, TF pills,
overlay toggles, C·A·R·D strip — untouched. `/api/chart/peers` payload, band-rule thresholds, GVM
number formatting (still 2 decimals) — untouched.

## Live checks still needed (Arpit)
Confirm on a real phone: rows no longer shift for Strong Buy peers, GVM+Band read as one grouped
unit, and the sort headers now look and feel tappable at a glance, on the V8, GVM and SmartGain
surfaces alike (same shared component, per the grep confirmation above).
