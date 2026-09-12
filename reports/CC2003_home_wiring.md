# cc#2003 — Home wiring: Derivatives removed, Max Pain chart opens the chain popup

Founder-revised spec, 12-Sep-2026 ~08:15 IST, on two Home screenshots. Supersedes the original
cc#2003 tap-target scope; resolves the open_question it had held ("last, after the basis table,
Index Intel" was never the intent — the intent was Derivatives removal + a popup chain).

## What this lands

**`mobile/home.html`**
1. The DERIVATIVES section is REMOVED, not hidden — the CSS (`.deriv-card`/`.deriv-row` family),
   the JS (`oiShort`, `derivExpiryTxt`, `DERIV`, `derivRowHtml`, `derivShellHtml`,
   `loadDerivatives`, `derivSheetOv`/`derivSheetClose`/`derivSheetOpen`, their keydown listener),
   and both call sites (`h += derivShellHtml();` and `loadDerivatives();`) are all deleted. Grepped
   the whole file afterward for every one of those identifiers plus `deriv-card`/`deriv-row`/
   `deriv-sheet` — zero remaining references outside this task's own explanatory comment.
   `.deriv-sheet-ov`/`.deriv-sheet` CSS is KEPT — confirmed still live, reused verbatim by the
   Approved Trades filter sheet (`apTrFilterOv()`, home.html) — deleting it would have broken an
   unrelated card.
2. The Max Pain chart area (`.oib`, inside `oiCard()`) is now a second, larger tap target:
   `onclick="chainPopupOpen('<symbol>')"`, opening the SAME popup cc#2004/the cc#2003 prerequisite
   push (sha `b201daf`) already built — one implementation, not a second copy. `name` (the
   parameter `oiCard()` already threads into the existing `oiRead(name)` (i) button) is exactly
   `IBK.sym` at every call site (`renderIbkDeck()` passes it in), confirmed by reading
   `renderIbkDeck()`/`ibkSetSym()` directly rather than assumed.
3. The pre-existing (i) button (`oiRead`) and the Index Intel link are untouched — both sit outside
   `.oib` in the markup (the (i) button is part of `header`, built before `.oib`; Index Intel is a
   sibling of `#heroCard3Body`), so the new handler cannot collide with either, per do_not_touch.

**`scorr_cockpit_card.js` — a defect in my OWN earlier cc#2004 push, found and fixed here**

cc#2003's own spec says "use existing theme tokens, no new literals (cc#1970/cc#1998 apply)" — so
before wiring the popup in, I checked `_dcChainRowBg()`/`_dcChainLegendHtml()`/the max-pain dot
against the ACTUAL bridge, not the docstring's word for it. Read `mobile/theme_mobile.css`'s cc#1941
comment directly: the complete `--c-*` family `#dcOv` (this popup's own shell) reads is
`bg/panel/bd/tx/mut/dim/grn/red/blu` — no `--c-gold`, no `--c-teal`. My cc#2004 push (sha `74f383d`)
had invented both, each with a hex fallback (`var(--c-gold, #D4AF37)`, `var(--c-teal, #2FD48B)`) —
since neither name is ever declared anywhere, the "fallback" was not a fallback, it was the
permanent value on every theme, on every set. `mcp__Scorr__theme_validate` (the same check the push
gate runs) doesn't currently track this file's fallback count against a baseline, so it wasn't
caught mechanically — but the spec's own instruction is explicit regardless of what the ratchet
enforces today.

Fixed to match what the REAL Max Pain chart already does for the identical three roles (grepped
home.html's own `.oiwall.cw`/`.oiwall.pw`/`.oiwall.mpw` CSS directly):
- Call wall: `var(--c-red)` — already correct (no fallback), unchanged.
- Put wall: was `var(--c-teal, #2FD48B)` → now `var(--c-grn)` (bridged, zero fallback) — matches
  `.oiwall.pw{color:var(--grn)}`, not "teal" (the spec offered "teal/green" as alternatives,
  "consistent with the colours already used on the Max Pain chart itself" as the deciding rule).
- Max pain: no bridged token exists for gold/amber anywhere in the `#dcOv` family (`--gold` belongs
  to a completely different `#gvp`/`#ckp`/`#v8p`-only bridge this popup is not part of). home.html's
  own chart hardcodes `#FF9F45` for this exact role and says so explicitly in its own comment
  (`home.html:787`: "the amber role ... has NO token") — a documented, accepted exception, not a
  gap I'm introducing. Reused byte-for-byte as `MP_AMBER`, named once and referenced from all three
  places that needed it (row background, legend dot, tap-to-detail marker) instead of retyping a
  literal three times.

## Strike range (spec item 4) — verified already satisfied, no change needed

Item 4 says the popup must show spot ±10 strikes (~21 rows), "SUPERSEDES cc#2004 item 3." Rather
than trust `option_chain_grid.py`'s own docstring claim that `strike_chain()` is "already ATM±10,"
read the actual source:
- Index path (`deriv_metrics.py:1701`): `strikes = sorted(sorted(all_strikes, key=lambda s: abs(s -
  spot))[:21])` — exactly 21 nearest-to-spot strikes.
- Stock path (`stock_options_backfill.py:140`, called with `each_side=10`):
  `strikes = sorted(...)[: 2 * each_side + 1]` = `[:21]` — same cap, same logic.

Both paths already return ≤21 strikes nearest spot before this task touched anything. No backend
change was needed; this is a confirmation, not an assumption; stated here so nothing downstream
re-opens it as unverified.

## Verify

- `node --check` clean on `scorr_cockpit_card.js` directly, and on `mobile/home.html`'s inline
  `<script>` blocks (extracted and checked as one file, same method used for check.html/v8.html
  earlier this session).
- Grep swept the whole file for every deleted identifier — zero stray references.
- Playwright, against the REAL edited files (repo root served locally, `mobile/home.html` loaded
  as-is, `scorr_cockpit_card.js` injected the way `main.py`'s server-side head injection does it in
  production, `/static/scorr_appshell.js` routed to its real repo-root file so `fetchWithTimeout`
  — which the whole boot sequence depends on — is genuinely defined, not stubbed):
  - `.deriv-card` / `.deriv-row` / `#derivBody`: **zero** in the rendered DOM.
  - `/api/mobile/home/derivatives`: **never fetched** (route spy, zero hits).
  - `.oib`: exactly one, carries `onclick="chainPopupOpen(...)"`, `cursor:pointer`.
  - Tapping it with the page toggled to NIFTY opens the popup for NIFTY (`hasVolumeSection:
    false`, confirming it's the minimal popup, not the full D-cockpit); the popup's own close
    button closes it.
  - Toggling to BANKNIFTY (`ibkSetSym('BANKNIFTY')`) and tapping again opens the popup for
    BANKNIFTY specifically, not a stale NIFTY — proves `name`/`IBK.sym` is read fresh per render,
    not captured once.
  - The pre-existing (i) buttons (`.oiib`, both the Max Pain card's `oiRead(...)` and the separate
    PCR/mood card's `pcrRead()`) and the Index Intel anchors are still present and unchanged.
  - Zero console/page errors from either file's own code across the whole run (the two errors the
    run shows — a 404 and a proxy-blocked external font connection — are pre-existing artifacts of
    the test harness's local static serving, not from anything this task touched).

## What this does NOT include

Item 7 (the Market Mood/PCR gauge tap target) is dropped from cc#2003's scope per the founder's
revision — that is cc#2006 now, separate and still pending. This task does not touch the Max Pain
chart's own rendering, data, the NIFTY/BANKNIFTY toggle, the PCR value, `cc#1859` tag computation,
or the Expert Curated (Approved Trades) carousel, per do_not_touch.

Founder glass-check (item in the spec's own verify list) is still outstanding — this session's
synthetic-data structural verification is as far as it can go without a live device. Card not
done — Fable verifies, per the standing workflow.
