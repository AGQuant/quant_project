# cc#2048 — option chain strike-detail panel: tag placement + a flagged semantic mismatch

Founder, dictated this session, on the CALL/PUT strike-detail panel: (1) if Fair value is higher
than the current premium, how can the tag say Expensive — and the tag should sit under the
option's current price (Premium), not under Fair value; (2) remove the OI-change / Bid-ask
not-available comment lines entirely.

## Step 0 — the file, located and confirmed before editing

The card itself said this front-end file "was NOT found in this session" and listed three files it
had already ruled out. It is `scorr_cockpit_card.js`'s `_dcChainDetailHtml()` — the exact function
this session already built twice (cc#2034 added the Greeks block; cc#2044 built the pop-out
mechanism and removed the Black-Scholes line). Confirmed by exact string match: the two
not-available sentences the founder quoted are verbatim at lines ~500-501 of that function. The
tag rendered is `ivp.tag` (cc#1859's IV-percentile mechanism), not the older `price_rows` tag —
confirmed by reading `leg()` directly, not assumed from the screenshot.

## Item 1 — the tag moved, exactly as asked

`fairLine` (the `Fair value ₹X (Tag)` line) had the tag word `_dcTagWord(ivpTag)` living inside it.
It is now a plain `Fair value <b>₹X</b>` line, tag-free. The tag word moved into a new
`premiumLine`, rendered beside `Premium <b>{ltp}</b>` exactly as it used to sit beside Fair value
(same parenthetical style, same colour) — display position only; `_dcTagWord()`/`ivp.tag` itself
is untouched.

## Item 2 — flagged, not silently resolved

The card is explicit that this is a decision point, not a default CC picks. Stated here and in
`cc_task_logs` for founder/Fable sign-off: **once beside Premium, this tag will read as "Premium
vs Fair value" to anyone looking at it — but it isn't.** `ivp.tag` is a percentile read of *today's
implied volatility against its own trailing 120-session history*; `ivp.fair_value` is a
Black-Scholes price built from that history's *median* IV. The two can legitimately disagree in
direction from Premium — the founder's own screenshot is exactly this: Premium 447.9, Fair value
448.5 (Premium below Fair value), tag still EXPENSIVE, because today's IV is elevated versus its
own history even though the median-based Fair value lands close to the live price. This is not a
bug in either number; it's two different, both-correct questions sitting on adjacent lines. Two
ways to resolve it, **neither implemented here**:
- **(a)** Keep the IV-percentile tag (it is cc#1859's own founder-ruled methodology) and add a
  short, honest qualifier so it can't be misread as a Premium-vs-Fair-value verdict — e.g. label it
  "IV: Expensive" or show the percentile number.
- **(b)** Switch this panel specifically to the *other* existing mechanism —
  `deriv_metrics.py._price_rows()`'s own `tag` field, a direct `(premium/fair − 1)` comparison —
  so the label actually matches the two numbers it now sits beside.

Neither was chosen. The tag reads exactly as it did before (same word, same source), just
repositioned, until the founder or Fable rules on (a) vs (b).

## Item 3 — the two not-available lines, removed

Per the founder's own words this session ("if something is not available, don't show, why show
comment") — an explicit, scoped exception to the general never-fabricate-silently doctrine, for
these two fields on this card only. The `OI change: not tracked...` and `Bid/ask: not captured...`
divs are deleted outright (not made conditional): `option_chain_grid.py`'s own docstring confirms
neither field exists anywhere in this pipeline yet, so there is no "sometimes present" case to
preserve — this is a front-end omission only, the backend's own never-fabricate stance at the data
layer is untouched.

**Untouched, per `do_not_touch`:** `option_ivp.py`'s methodology (window, bands, bucket skew —
cc#1859, session_log 6264-6312), `option_chain_grid.py`'s data-layer omission of OI-change/bid-ask,
the Option Greeks block, the max-pain/call-wall/put-wall legend, and the DTE/as-of footer.

## Verify

`node --check` clean. Real headless Chromium, the actual `scorr_cockpit_card.js` +
`scorr_card_common.js`, a fixture reproducing the founder's own exact screenshot numbers (Premium
447.9, Fair value 448.5, tag EXPENSIVE) served through the real `ScorrCockpitCard.openChain`
entry point — **14/14 checks pass**:

- Premium line now reads `447.9 (Expensive)`; Fair value line reads a bare `₹448.5`, no tag anywhere
  near it. Confirmed for both legs (PE: `95.0 (Cheap)` / `Fair value ₹93.5`).
- `"OI change"`, `"not tracked"`, `"Bid/ask"`, `"not captured"` do not appear anywhere in the panel's
  rendered text.
- Sanity check that item 2 was genuinely left unresolved: no new qualifier text (`"IV:"`,
  `"IV Expensive"`) appears anywhere — the tag word is still the plain, unmodified `ivp.tag` output.
- OI line, the full Greeks block, the DTE/as-of footer, and the already-removed Black-Scholes line
  all confirmed exactly as `do_not_touch` requires. Zero console/page errors.

Not done here, and not needed for this card: the founder's own live check on scorr.in (this
container has no route to the deployed site). Item 2's ruling is also not done here by design —
posted to `cc_task_logs` for Fable/founder sign-off per the card's own explicit instruction.
