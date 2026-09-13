# cc#2034 — dots removed, Option Greeks added to the strike-chain tap panel

Founder voice, 13-Sep-2026. Two changes to the same popup: (1) remove the Cheap/Fair/Expensive
dot next to every CE/PE LTP cell — "too many dots on screen at once read as noise, not signal";
(2) add delta/gamma/theta/vega to the existing tap-a-strike detail panel, in a visually separate
"Option Greeks" section, and reuse the same numbers on the derivative cockpit's own ATM block
(`also_applies_to: derivative_cockpit`).

## Why Sonnet built the engine half of this card

The card reads as UI+ENGINE (new Greeks functions live in `deriv_metrics.py`, not just markup).
Held-for-Fable cards this session (cc#2031 Black-76, cc#2032 Beta engine, cc#2033 basket rollup,
cc#2035 Strategy Builder) all involve a genuine open financial-engineering judgment call — a
methodology choice with more than one defensible answer. This card does not: it names the exact,
standard, closed-form Black-Scholes Greeks formulas (the same `S`/`K`/`T`/`sigma`/`r` this file's
own `_bs_price`/`_bs_iv` already use), not a new methodology to invent. Grepped the real chain-grid
wiring first, per the card's own instruction, before writing anything.

## `deriv_metrics.py` — four new closed-form functions, `_bs_price`/`_bs_iv` untouched

`_bs_delta`, `_bs_gamma`, `_bs_vega`, `_bs_theta` — standard Black-Scholes, each `None`-safe on
bad/missing input exactly like the existing `_bs_iv`. Delta differs by leg (`N(d1)` for CE,
`N(d1)-1` for PE — put-call delta parity, `delta_CE - delta_PE = 1` exactly). Gamma and vega are
leg-independent (same formula both sides). Theta differs by leg (opposite-signed second term).
Vega is reported per 1% IV move (raw/100, the market's own convention); theta per calendar day
(annualised/365). A new `_r(x, n)` None-safe round sits next to the existing `_f(x)` — every
`_bs_*` already returns `None` on bad input, so this just carries that through instead of
`round()`-ing a `None`.

**`_bs_price`/`_bs_iv` are byte-identical to before this card** — the two functions this whole
platform's option fair-value already depends on were read, not touched.

Wired into `_price_rows()` (the one shared row-builder both the index and stock chain paths already
call, per this project's own `ONE_REGISTRY_ONE_DERIVATION_V1` doctrine — computed once, consumed
everywhere) using each leg's own already-solved `iv` as sigma. A leg with no live quote has no `iv`
to solve from, so its `greeks` is `null` — an honest gap, never a fabricated number. Also wired into
`_options_block()`'s returned `"atm"` dict (CE+PE), per the card's own `also_applies_to:
derivative_cockpit` line — same functions, same rule, no duplicate formula for the second surface.

## Verification method — finite-difference against this codebase's own shipped pricer

A first check against remembered textbook (Hull) reference figures for delta/vega/theta did not
match, while the option PRICE computed from the identical `d1`/`d2` matched closely. That is the
signature of a misremembered reference number, not a formula bug — memory is not a primary source,
so it was not trusted. Re-verified instead by differentiating the codebase's OWN `_bs_price`
numerically (delta=∂Price/∂S, gamma=∂²Price/∂S², vega=∂Price/∂σ, theta=-∂Price/∂T) across 3 realistic
scenarios (ITM/ATM/OTM, CE+PE) — **18/18 checks agree to ~1e-6**. Re-run a second time directly
against the actual imported shipped module (`import deriv_metrics as dm`, post-edit) — **12/12
agree to ~1e-6**, confirming zero transcription error between the verified scratch formula and what
actually shipped. Put-call delta parity checked explicitly and holds exactly (0.5030 −
(−0.4970) = 1.0000 in the fixture below).

## `scorr_cockpit_card.js` — dots out, two-section detail panel in

- `_dcTagDot(leg)` removed entirely (was called from both the CE and PE `<td>` in every chain row);
  its two call sites now render a bare LTP number, nothing appended.
- `_dcChainLegendHtml`: the Cheap/Fair/Expensive dot key removed. The **Max pain / Call wall / Put
  wall legend is untouched** — those are full-row background colours, unrelated to the removed dot,
  and still render exactly as before.
- A new one-line hint — "Tap a strike for fair value & Greeks" — added directly under the
  spot/expiry header row, replacing the read-at-a-glance cue the dots used to carry.
- `_dcChainInfoHtml()` — the stale "Dot colour" paragraph (describing a control that no longer
  exists) replaced with a "Tap a strike" explanation. Max pain / Call wall+Put wall paragraphs and
  the "Not a trading signal" disclaimer are untouched.
- `_dcChainDetailHtml`'s per-leg `leg()` renderer rewritten into the founder's two-section layout:
  Section 1 (Premium / IV / Fair value+tag / BS fair / OI / OI-change-not-tracked / bid-ask-not-
  captured) unchanged in content, followed by a divider and a literal **"Option Greeks"** heading,
  under which Delta/Gamma/Theta/Vega render only when `o.greeks` is non-null — otherwise "Not
  available — no live quote to solve IV from", never a blank line. A leg whose `ivp.fair_value` is
  `null` (below cc#1859's own session floor) shows "Not enough history yet to rate this strike"
  instead of a blank or guessed tag — these two null-paths are independent of each other (one leg
  can be missing history but still have live Greeks, or vice versa; both are exercised separately
  below). The shared footer line gained DTE alongside the existing as-of timestamp.

## Verify — real Chromium, the real files, the real endpoint contract, not reimplemented

`ast.parse` clean on `deriv_metrics.py`; `node --check` clean on `scorr_cockpit_card.js`. Real
headless Chromium loaded the actual `scorr_card_common.js` + the actual edited
`scorr_cockpit_card.js`, called the real global entry point a host page uses
(`chainPopupOpen('NIFTY')`), with `/api/deriv/chain-grid/NIFTY` intercepted via `page.route` —
confirmed the real network call fired, not a stub read of a local variable. Fixture: 5 strikes
(put wall, plain, ATM+max pain, one leg with `ivp.fair_value=null`, one leg with `greeks=null`,
call wall), full CE/PE Greeks on the rest. **42/42 checks pass:**

- No dot markup remains in any CE/PE LTP cell across all 5 rows (regex: bare number only).
- The new "Tap a strike for fair value & Greeks" hint renders.
- Max pain / call wall / put wall row backgrounds still apply, untouched by this card.
- The legend still shows Max pain / Call wall / Put wall, and no longer carries a
  Cheap/Fair/Expensive key.
- Tapping the ATM strike (21800, fully populated) reveals both legs' Premium/IV/₹-marked Fair
  value/DTE, then a literal "Option Greeks" heading (2×, one per leg) with Delta/Gamma/Theta/Vega
  correctly formatted (4dp/6dp/2dp/2dp) and correctly labelled ("/ day", "/ 1% IV") — and the
  Fair-value line for each leg is confirmed, by string position, to render BEFORE that leg's own
  "Option Greeks" heading (the two sections are not interleaved).
- The `ivp.fair_value=null` leg (21850 CALL) shows "Not enough history yet to rate this strike" —
  while its own Greeks still render normally, and the SAME strike's PUT leg (ivp populated) shows
  a real tagged fair value — proving the two null-paths are independent per leg, not a whole-row
  fallback.
- The `greeks=null` leg (21900 PUT) shows "Not available — no live quote to solve IV from" — while
  its own Fair value still renders, and the SAME strike's CALL leg (greeks populated) still shows
  real Delta/Gamma/Theta/Vega.
- Tapping the same strike twice still closes the panel (pre-existing toggle behaviour, confirmed
  unaffected).
- The (i) info toggle no longer mentions "Dot colour", now explains "Tap a strike", and still
  explains Max pain / Call wall+Put wall / carries the disclaimer — all pre-existing content beyond
  the one stale paragraph is confirmed untouched.

## Not done here (out of this card's scope)

The derivative-cockpit ATM block's own on-page rendering of these same Greeks (only the backend
`_options_block()` payload was wired — the card's `also_applies_to` line asked for the data to be
available there, not a second UI build; `openDerivCockpit`'s own sheet markup is unrelated existing
code, untouched). A live check on scorr.in — no route to prod from this container; the 42/42
functional pass plus the finite-difference verification above are the evidence available here.
